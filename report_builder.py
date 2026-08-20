# -*- coding: utf-8 -*-
r"""文字起こしを★全文読んでレポートを組み立てるための、純粋な部分。

★2026-08-20 追加。それまでは一定の長さを超えると
  先頭4000字 / 中間4000字 / 末尾4000字 の★3か所を抜き取るだけで、
  残りを一度も読まなかった。今日の2時間の講演では★77.4%が読まれず、
  24章のうち17章が読まれない区間にあった。
  ★欠落を告げる仕組みが無いので、出力は正常に見えていた。

■ ここに置く理由
  Streamlit の画面と混ぜると試験できない。★API を1回も呼ばない部分だけを
  ここに集めて、実データで確かめられるようにする。

■ ★数え方の約束（ここが崩れると、また黙って落ちる）
  ・本文は★重なりの無い連続した区画（body）に分ける。区画の長さの合計は
    必ず元の長さと一致する。
  ・前の区画の末尾は「文脈」として渡すが、★読んだ量には数えない。
    重なりを二重に数えて「全部読んだ」と申告しないため。
  ・読めた区画だけを「読んだ」に数える。失敗した区画は★捨てた量に入る。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

for _sl in (Path(__file__).resolve().parent.parent / "shared-lib",
            Path(r"C:\dev\shared-lib"),
            Path(r"C:\Users\ohgai\dev\shared-lib")):
    if _sl.is_dir():
        if str(_sl) not in sys.path:
            sys.path.insert(0, str(_sl))
        break

import api_prices                                            # noqa: E402

#: 1区画の本文の長さ。今日の実測（53,171字 / 128.2分 ≒ 415字/分）で
#  おおよそ10分ぶんにあたる。★短くすると区画が増え、指示文の再送で費用が伸びる。
CHUNK_CHARS = 4300
#: 前の区画から文脈として渡す長さ。★読んだ量には数えない。
CONTEXT_CHARS = 400

#: 1回の実行で使ってよい上限（円）。★超えたら止める。抜き取りに倒れない。
COST_LIMIT_JPY = 300

#: 音声認識に渡す語彙ヒントの上限（文字）。whisper の prompt は 224 トークンまで。
#  ★日本語は1トークンが1字前後なので、安全側で少なめに取る。
VOCAB_PROMPT_MAX_CHARS = 240


class CostLimitExceeded(RuntimeError):
    """★見込みが上限を超えた。抜き取りに倒さず、ここで止める。"""


@dataclass
class Chunk:
    index: int
    start: int          # 元テキストでの本文の開始位置
    end: int            # 同・終了位置（この区画の本文は text[start:end]）
    body: str           # ★読んだ量に数えるのはここだけ
    context: str        # 直前の区画の末尾（数えない）

    @property
    def chars(self) -> int:
        return len(self.body)


def split_for_reading(text: str, chunk_chars: int = CHUNK_CHARS,
                      context_chars: int = CONTEXT_CHARS) -> list[Chunk]:
    """★全文を、重なりの無い連続した区画に分ける。

    行の切れ目で分ける（文字起こしは1行1発言なので、行の途中で切らない）。
    ★区画の長さの合計は必ず元の長さと一致する。ここが崩れたら数えが嘘になる。
    """
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    chunks: list[Chunk] = []
    buf: list[str] = []
    start = 0
    pos = 0
    for ln in lines:
        buf.append(ln)
        pos += len(ln)
        if pos - start >= chunk_chars:
            body = "".join(buf)
            chunks.append(Chunk(len(chunks), start, start + len(body), body,
                                text[max(0, start - context_chars):start]))
            start += len(body)
            buf = []
    if buf:
        body = "".join(buf)
        chunks.append(Chunk(len(chunks), start, start + len(body), body,
                            text[max(0, start - context_chars):start]))
    # ★数えの検算。ここが合わないなら分け方が壊れている。
    total = sum(c.chars for c in chunks)
    if total != len(text):
        raise AssertionError(
            "★区画の合計 %d字 が元の %d字 と一致しません（数えが嘘になります）"
            % (total, len(text)))
    return chunks


@dataclass
class ChunkResult:
    index: int
    ok: bool
    text: str = ""
    finish_reason: str = ""
    error: str = ""
    in_tokens: int | None = None
    out_tokens: int | None = None


def coverage(text_len: int, chunks: list[Chunk],
             results: list[ChunkResult]) -> dict:
    """★読んだ量と捨てた量を数える。申告ではなく、区画の長さから数える。"""
    by_i = {r.index: r for r in results}
    read = 0
    missing: list[Chunk] = []
    truncated: list[int] = []
    for c in chunks:
        r = by_i.get(c.index)
        if r is not None and r.ok and r.text.strip():
            read += c.chars
            if r.finish_reason == "length":
                truncated.append(c.index)
        else:
            missing.append(c)
    return {
        "total_chars": text_len,
        "read_chars": read,
        "dropped_chars": text_len - read,
        "ratio": (read / text_len) if text_len else 1.0,
        "chunks": len(chunks),
        "chunks_ok": len(chunks) - len(missing),
        "missing": [{"index": c.index, "start": c.start, "end": c.end,
                     "chars": c.chars} for c in missing],
        "truncated": truncated,
        "complete": read == text_len and not truncated,
    }


def coverage_section(cov: dict, source_note: str = "") -> str:
    """★読んだ量と捨てた量を、レポート自身に書く節。

    ★投入前の関所でもここを見る。数字が無いレポートは通さない、が使える形。
    """
    lines = ["## 読んだ量と捨てた量", ""]
    if source_note:
        lines += [source_note, ""]
    lines += [
        "| 項目 | 値 |",
        "|---|---|",
        "| 入力（文字起こし全体） | %s字 |" % f"{cov['total_chars']:,}",
        "| ★読んだ量 | %s字（%.1f%%） |" % (f"{cov['read_chars']:,}",
                                        cov["ratio"] * 100),
        "| ★捨てた量 | %s字 |" % f"{cov['dropped_chars']:,}",
        "| 区画 | %d本中 %d本を読了 |" % (cov["chunks"], cov["chunks_ok"]),
    ]
    if cov["complete"]:
        lines += ["", "★全文を読んで作成しました（捨てた区間はありません）。"]
    else:
        lines += ["", "★このレポートには**読まれていない区間があります**。"]
        for m in cov["missing"]:
            lines.append("- 区画%d（元テキストの %s〜%s字・%s字）が読めませんでした"
                         % (m["index"], f"{m['start']:,}", f"{m['end']:,}",
                            f"{m['chars']:,}"))
        for i in cov["truncated"]:
            lines.append("- ★区画%d は出力の上限で途中までしか書けていません" % i)
    return "\n".join(lines)


def estimate_plan_jpy(total_chars: int, material_chars: int,
                      prompt_chars: int, n_chunks: int,
                      out_chars_per_chunk: int, model: str = "gpt-4o",
                      db_path=None) -> dict:
    """★実行前の見込み額。区画ごとに資料と指示文を再送する分も数える。"""
    in_chars = total_chars + (material_chars + prompt_chars) * n_chunks
    out_chars = out_chars_per_chunk * n_chunks
    e = api_prices.estimate_jpy(model, in_chars, out_chars, db_path=db_path)
    e["n_chunks"] = n_chunks
    e["limit_jpy"] = COST_LIMIT_JPY
    e["over_limit"] = e["jpy"] > COST_LIMIT_JPY
    return e


def cost_note(e: dict) -> str:
    """★画面に出す見込みの明細。使った前提を隠さない。"""
    return (
        "見込み **%.0f円**（$%.2f）／上限 %d円\n\n"
        "- 区画 %d本・入力 約%s字・出力 約%s字\n"
        "- 単価: %s（%s 時点で確認）\n"
        "- ドル円 %.2f（%s・%s）\n"
        "- ★トークン数は文字数からの**見積り**です（1トークン≒%.1f字として計算）"
        % (e["jpy"], e["usd"], e["limit_jpy"], e["n_chunks"],
           f"{e['in_chars']:,}", f"{e['out_chars']:,}",
           e["price_source"], e["price_checked_on"],
           e["usd_jpy"], e["usd_jpy_on"], e["usd_jpy_src"],
           e["chars_per_token"]))


# ── 語彙ヒント（音声認識に渡す）────────────────────────────

#: ★固定で書き込まない。ここを読む。無ければ空で動く（黙って止めない）。
VOCAB_FILES = ("vocab/legal_ja.txt",)


def load_vocab(base_dir: Path, files=VOCAB_FILES, extra_path: str = "") -> list[str]:
    """★語彙は外のファイルから読む。あとから足せるようにするため。

    `#` で始まる行と空行は無視する。順序は書かれた順（先頭ほど優先）。
    """
    terms: list[str] = []
    paths = [Path(base_dir) / f for f in files]
    if extra_path:
        paths.append(Path(extra_path))
    for p in paths:
        try:
            raw = p.read_text(encoding="utf-8")
        except Exception:                                    # noqa: BLE001
            continue
        for line in raw.splitlines():
            t = line.strip()
            if t and not t.startswith("#") and t not in terms:
                terms.append(t)
    return terms


def whisper_prompt(terms: list[str],
                   max_chars: int = VOCAB_PROMPT_MAX_CHARS) -> tuple[str, int, int]:
    """★語彙ヒントを組み立てる。返す: (文字列, 使った数, ★入らなかった数)。

    ★入らなかった数を返すのは、黙って捨てないため。呼び出し側が画面に出す。
    """
    used: list[str] = []
    for t in terms:
        cand = "、".join(used + [t]) + "。"
        if len(cand) > max_chars:
            break
        used.append(t)
    if not used:
        return "", 0, len(terms)
    return "、".join(used) + "。", len(used), len(terms) - len(used)


# ── 時刻（verbose_json のセグメント）────────────────────────

def timestamped_text(segments: list, offset_sec: float = 0.0) -> str:
    """★[HH:MM:SS] を各行の頭に付けた文字起こしを作る。

    時刻が本文に入るので、この後の区画・章・欠落の位置がすべて時刻で言える。
    """
    out = []
    for s in segments or []:
        t = float(s.get("start", 0)) + offset_sec
        out.append("[%s] %s" % (hhmmss(t), (s.get("text") or "").strip()))
    return "\n".join(out)


def hhmmss(sec: float) -> str:
    s = int(sec)
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def silent_gaps(segments: list, min_sec: float = 8.0,
                offset_sec: float = 0.0) -> list[dict]:
    """★音が途切れた区間を、時刻から機械的に拾う。

    人が聞き直さなくても「どこが聞き取れていないか」を件数と位置で言える。
    """
    gaps = []
    prev = None
    for s in segments or []:
        st = float(s.get("start", 0)) + offset_sec
        en = float(s.get("end", 0)) + offset_sec
        if prev is not None and st - prev >= min_sec:
            gaps.append({"from": prev, "to": st, "sec": st - prev,
                         "from_hhmmss": hhmmss(prev), "to_hhmmss": hhmmss(st)})
        prev = en
    return gaps


def gaps_section(gaps: list[dict]) -> str:
    if not gaps:
        return "## 聞き取れなかった箇所\n\nなし（%.0f秒以上の空白は検出されませんでした）。" % 8
    lines = ["## 聞き取れなかった箇所", "",
             "★%d件（音声の空白区間。時刻から機械的に抽出）" % len(gaps), "",
             "| # | 位置 | 長さ |", "|---|---|---|"]
    for i, g in enumerate(gaps, 1):
        lines.append("| %d | %s〜%s | %.0f秒 |"
                     % (i, g["from_hhmmss"], g["to_hhmmss"], g["sec"]))
    return "\n".join(lines)


# ── 組み立て（★ここで要約しない。連結するだけ）──────────────

def assemble(title: str, overview: str, chapters: list[str],
             cov: dict, gaps: list[dict] | None = None,
             source_note: str = "") -> str:
    """★本編は連結するだけ。ここに LLM を通さない。

    通すと、せっかく全文を読んだものが★もう一度要約されて落ちる。
    「全体像」だけは別に作るが、材料は章の見出しに限る（呼び出し側の責任）。
    """
    parts = ["# %s" % title, ""]
    if overview:
        parts += ["## 全体像", "", overview.strip(), ""]
    parts += ["## 本編", ""]
    for c in chapters:
        if c and c.strip():
            parts += [c.strip(), ""]
    parts += [coverage_section(cov, source_note), ""]
    if gaps is not None:
        parts += [gaps_section(gaps), ""]
    return "\n".join(parts).rstrip() + "\n"


_HEADING = re.compile(r"^#{2,4}\s*(.+?)\s*$", re.M)


def chapter_titles(chapters: list[str]) -> list[str]:
    """★「全体像」を作る材料。本文ではなく見出しだけを渡すために使う。"""
    out = []
    for c in chapters:
        for m in _HEADING.finditer(c or ""):
            t = m.group(1).strip()
            if t and t not in out:
                out.append(t)
    return out
