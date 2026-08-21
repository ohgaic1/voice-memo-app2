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
#: ★和暦の漢数字を読むのに使う。関所の判定と同じ実装を使う（二重に持たない）。
from gate_checks import kanji_to_int                          # noqa: E402

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


# ── 区画の出力を機械的に読み取る（★ここで LLM を使わない）──────────
#
#   ★区画ごとに作るのは「本文を読まないと作れないもの」だけにする。
#     区画をまたいで揃えるだけのもの（一覧）は、★機械的に連結する。
#     LLM を通す回数を最小にして、再要約の余地を作らないため。

#: 区画の出力の中で、章の後ろに置く一覧の区切り。
MARK_TERMS = "<<<用語の訂正>>>"
MARK_NUMS = "<<<日付・数値>>>"
MARK_LAWS = "<<<条文・法令>>>"
#: ★公表状態。確信度（「言い切られていないこと」）とは別物。
#  ★2026-08-21 実測: 文字起こしに「オフレコ」「フライング」「まだどこにも」が
#    あるのに、レポート本文では全部0件だった。指示に項目が無かったため。
#    該当箇所は区画1に入っており、その区画は読まれていた（13/13・100%）。
#    ★読んだのに落ちる形だった。
MARK_PUB = "<<<公表状態>>>"
MARKS = (MARK_TERMS, MARK_NUMS, MARK_LAWS, MARK_PUB)


#: ★指示文の見出しをそのまま書き写してくることがある（2026-08-20 実測）。
#  「事項 | 値 | 明確 or 推定 or 不確か」がそのまま1行目に入っていた。
_HEADER_WORDS = ("明確 or 推定", "直す前 → 直した後", "直す前 →",
                 "法令名 | 条", "事項 | 値")


#: ★一覧の中に本文が混ざったことを見分ける手がかり。
#  章の項目名が出てきたら、そこから先は一覧ではなく本文。
_BODY_MARKS = ("**何が変わるのか", "**講師が述べた根拠", "**実務でどう効くか",
               "**講師が示した具体例", "**言い切られていないこと**")


def _looks_like_body(t: str) -> bool:
    return any(t.startswith(w) for w in _BODY_MARKS)


def _is_header_row(t: str) -> bool:
    return any(w in t for w in _HEADER_WORDS)


def _is_noop_correction(t: str) -> bool:
    """★「特定補助 → 特定補助」のように直っていない行を落とす。

    2026-08-20 実測: 訂正一覧に、左右が同じ行が混ざっていた。
    訂正していないものを訂正として数えると、件数が嘘になる。
    """
    for arrow in ("→", "->", "⇒"):
        if arrow in t:
            a, _, b = t.partition(arrow)
            if a.strip() and a.strip() == b.strip():
                return True
    return False


def parse_chunk_output(text: str) -> dict:
    """★区画の出力を、章の本文と3つの一覧に分ける。

    返す: {"chapters": str, "terms": [...], "numbers": [...], "laws": [...]}
    ★印が無ければ空の一覧にする（無い物を作らない）。
    """
    s = text or ""
    pos = [(s.find(m), m) for m in MARKS if s.find(m) >= 0]
    pos.sort()
    chapters = s[:pos[0][0]].strip() if pos else s.strip()
    out = {"chapters": chapters, "terms": [], "numbers": [], "laws": [],
           "publicity": []}
    key = {MARK_TERMS: "terms", MARK_NUMS: "numbers", MARK_LAWS: "laws",
           MARK_PUB: "publicity"}
    for i, (p, m) in enumerate(pos):
        end = pos[i + 1][0] if i + 1 < len(pos) else len(s)
        body = s[p + len(m):end]
        rows, spilled = [], []
        back_to_body = False
        for line in body.splitlines():
            t = line.strip().lstrip("-・ ").strip()
            if t.startswith("|"):
                t = t.strip("|").strip()
            # ★2026-08-21: 印より後ろに章が戻ってきたら、そこから先は★本文に返す。
            #   実測: 最後の区画が <<<公表状態>>> の後ろに章を書き、
            #   その5章ぶん62行が「公的な裏付けが無い箇所」の表に流れ込み、
            #   件数が 4件 → ★66件 と表示された。
            #   ★一覧に本文が混ざると、件数が嘘になるうえ、本編から章が消える。
            if back_to_body or _HEADING.match(t) or _looks_like_body(t):
                back_to_body = True
                spilled.append(line)
                continue
            if not t or t.startswith("|--") or t in ("なし", "（なし）"):
                continue
            if _is_header_row(t) or _is_noop_correction(t):
                continue          # ★見出しの写しと、直していない行は落とす
            rows.append(t)
        out[key[m]] = rows
        if spilled:
            # ★捨てない。本文へ戻す（勝手に消さない）。
            out["chapters"] = (out["chapters"] + "\n\n"
                               + "\n".join(spilled)).strip()
            out.setdefault("spilled", 0)
            out["spilled"] += len(spilled)
    return out


def merge_rows(parsed: list[dict], key: str) -> list[str]:
    """★区画をまたいで一覧を揃える。順序は出てきた順、重複は落とすだけ。"""
    seen, out = set(), []
    for p in parsed:
        for r in p.get(key) or []:
            k = r.replace(" ", "").replace("　", "")
            if k not in seen:
                seen.add(k)
                out.append(r)
    return out


def chapters_missing_heading(parsed: list[dict], results) -> list[int]:
    """★破綻点の検知: 章の見出しが1つも無い区画を拾う。

    プロンプトを変えたときに出力が章の形から外れても、連結すれば
    ★見た目は通ってしまう。読了率は 100% のままなので気づけない。
    """
    bad = []
    for r, p in zip(results, parsed):
        if not getattr(r, "ok", False):
            continue
        if not _HEADING.search(p.get("chapters") or ""):
            bad.append(r.index)
    return bad


def terms_section(rows: list[str]) -> str:
    if not rows:
        return "## 用語の訂正\n\nなし（訂正した用語はありません）。"
    return "\n".join(["## 用語の訂正", "",
                      "音声認識の誤変換を配付資料と突き合わせて直したもの。", "",
                      "| 直す前 → 直した後 |", "|---|"]
                     + ["| %s |" % r for r in rows])


def numbers_section(rows: list[str]) -> str:
    if not rows:
        return "## 日付・数値の一覧\n\nなし（日付・数値への言及はありません）。"
    return "\n".join(["## 日付・数値の一覧", "",
                      "★施行日・条文番号・法令名は法令DBとの突合前提。", "",
                      "| 事項・値・確からしさ | 印 |", "|---|---|"]
                     + ["| %s | ★要突合 |" % r for r in rows])


def laws_section(rows: list[str]) -> str:
    if not rows:
        return "## 条文・法令への言及\n\nなし（条文・法令への言及はありません）。"
    return "\n".join(["## 条文・法令への言及", "",
                      "★聞き取りである以上、番号は誤りうる。すべて要突合。", "",
                      "| 法令名・条・文脈 | 印 |", "|---|---|"]
                     + ["| %s | ★要突合 |" % r for r in rows])


def publicity_section(rows: list[str]) -> str:
    """★公的な裏付けが無い箇所を、本文とは分けて出す節。

    ★これは「入れてはいけない」という意味ではない。入れるかは人が決める。
      ★勝手に本文へ混ぜない・勝手に捨てない、の両方を満たすための置き場。
    ★0件のときに「無い」と言い切らない。拾えなかっただけかもしれない。
    """
    if not rows:
        return ("## ★公的な裏付けが無い箇所\n\n"
                "★拾えたのは0件です。\n"
                "※ 0件は「無い」という意味ではありません。"
                "講師が公表状態に触れなかったか、拾えなかったかのどちらかです。")
    return "\n".join(
        ["## ★公的な裏付けが無い箇所", "",
         "★%d件。講師が「まだ公にしていない」と断った箇所です。" % len(rows),
         "★入れてはいけないという意味ではありません。"
         "知識ベースに入れるかどうかを、ここを見て決めてください。", "",
         "| 主題・発言・時刻 |", "|---|"]
        + ["| %s |" % r for r in rows])


def assemble_full(about: str, overview: str, chapters: list[str],
                  terms: list[str], numbers: list[str], laws: list[str],
                  cov: dict, gaps: list[dict] | None,
                  next_steps: str = "", source_note: str = "",
                  no_heading: list[int] | None = None,
                  num_found: dict | None = None,
                  publicity: list[str] | None = None) -> str:
    """★決められた順に組み立てる。本編は連結するだけ（要約しない）。"""
    parts = ["# 講演の記録", ""]
    parts += ["## 1. この記録について", "",
              (about or "不明").strip(), "",
              "★この記録は講演の記録であり、条文そのものではありません。"
              "条文・法令名・数値は必ず一次資料に当たってください。", ""]
    if source_note:
        parts += [source_note, ""]
    parts += ["## 2. 全体像", "", (overview or "（生成できませんでした）").strip(), ""]
    parts += ["## 3. 本編", ""]
    for c in chapters:
        if c and c.strip():
            parts += [c.strip(), ""]
    if no_heading:
        parts += ["★次の区画は、章の形になっていない出力が混じっています: %s"
                  % ", ".join("区画%d" % i for i in no_heading), ""]
    parts += [numbers_section(numbers), ""]
    parts += [laws_section(laws), ""]
    parts += [terms_section(terms), ""]
    # ★本文には混ぜず、判断の対象として分けて出す。
    parts += [publicity_section(publicity or []), ""]
    parts += [gaps_section(gaps or []), ""]
    if num_found is not None:
        parts += [numbers_check_section(num_found, chapters), ""]
    parts += [coverage_section(cov), ""]
    if next_steps:
        parts += ["## 事務所として次に確かめること", "", next_steps.strip(), ""]
    return "\n".join(parts).rstrip() + "\n"


#: ★章が他の章を参照している疑いのある語。知識ベースで章だけ読むと意味が通らなくなる。
CROSS_REF_WORDS = ("前述の", "上記の", "先ほどの", "前章", "既述の", "後述の")


def cross_reference_hits(chapters: list[str]) -> list[tuple[int, str]]:
    """★章が他の章を参照していないかを見る。返す: [(章の番号, 見つかった語)]。

    ★この記録は知識ベースに入り、章だけが引かれる。
      「前述のとおり」と書かれていると、その章だけでは意味が通らない。
    """
    hits = []
    for i, c in enumerate(chapters):
        for w in CROSS_REF_WORDS:
            if w in (c or ""):
                hits.append((i, w))
    return hits


def estimate_whisper_jpy(minutes: float, db_path=None) -> dict:
    """★音声認識の見込み額。分あたりの単価は api_prices が持っている。

    ★2026-08-20 追加。それまで関所はレポート生成の分しか見ておらず、
      文字起こしの費用が「1回あたり」から漏れていた。
    """
    rate, on, src = api_prices.usd_jpy(db_path)
    usd = minutes * api_prices.WHISPER_USD_PER_MIN
    return {"minutes": minutes, "usd": usd, "jpy": usd * rate,
            "usd_jpy": rate, "usd_jpy_on": on, "usd_jpy_src": src,
            "limit_jpy": COST_LIMIT_JPY, "over_limit": usd * rate > COST_LIMIT_JPY,
            "price_source": api_prices.PRICE_SOURCE,
            "price_checked_on": api_prices.PRICE_CHECKED_ON,
            "is_estimate": True}


def compression_note(info: dict) -> str:
    """★音質を落としたなら、落としたことをレポートに書く。

    2026-08-21 追加。それまでは全体を 32kbps へ再エンコードしてから分割しており、
    ★落としたこと自体がどこにも残らなかった。
    """
    n = len(info.get("compressed") or [])
    total = info.get("chunks") or 0
    if not n:
        return ""
    return ("★音声の %d区画中 %d区画（%s）は上限（24MB）を超えたため、"
            "32kbps に圧縮してから認識しています。その区画は音質が落ちています。"
            % (total, n, ", ".join("区画%d" % i for i in info["compressed"])))


# ── 本文に数値が残っていないかを機械的に見る ────────────────────
#
#   ★2026-08-21 実測: 「日付・数値は本文に書かない」と指示しても守られなかった。
#     35行に数字が残り、内訳は タイムスタンプの写し込み14行 / 日付2行 /
#     条文番号7行 / ★用語に含まれる数字4行 / その他8行 だった。
#     しかも「（→日付・数値の一覧）」という参照は★0回。
#   ★指示を厳しくするだけでは足りない。出た物を数えて表に出す。

#: ★これは「数値」ではなく用語の一部。書いてよい（書きようがない）。
#  第2種社会福祉事業 / 9条列挙行為 / 3類型 / 2本柱 / 2段構え など。
TERM_DIGIT = re.compile(
    r"第\s*[0-9０-９]+\s*種|[0-9０-９]+\s*条列挙行為|[0-9０-９]+\s*類型"
    r"|[0-9０-９]+\s*本柱|[0-9０-９]+\s*段構え|[0-9０-９]+\s*号行為")

#: ★本文から外すべきもの。
PAT_DATE = re.compile(r"令和\s*[0-9０-９]+\s*年|[0-9０-９]+\s*月\s*[0-9０-９]+\s*日")
PAT_ARTICLE = re.compile(r"[0-9０-９]+\s*条(?!列挙)")
PAT_TIMESTAMP = re.compile(r"\[[0-9]{2}:[0-9]{2}:[0-9]{2}\]")


def numbers_in_body(text: str) -> dict:
    """★本文に残っている数値を種類ごとに数える。

    ★用語に含まれる数字（第2種・3類型など）は数えない。書きようがないため。
    返す: {"date": [...], "article": [...], "timestamp": [...], "total": n}
    """
    out = {"date": [], "article": [], "timestamp": []}
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s or s.startswith("**"):
            continue
        if PAT_TIMESTAMP.search(s):
            out["timestamp"].append(s[:60])
        masked = TERM_DIGIT.sub("", PAT_TIMESTAMP.sub("", s))
        if PAT_DATE.search(masked):
            out["date"].append(s[:60])
        elif PAT_ARTICLE.search(masked):
            out["article"].append(s[:60])
    out["total"] = sum(len(v) for k, v in out.items() if k != "total")
    return out


#: ★数値を本文から外したときに、章に残すべき参照。
REF_MARK = "→日付・数値の一覧"


def numbers_check_section(found: dict, chapters: list[str]) -> str:
    """★本文に数値が残っていたら、レポート自身に出す。黙って通さない。

    ★破綻点への手当ても兼ねる: 数値を外したのに参照も無い章は、
      章だけを読んでも「いつから」が分からない。その数も出す。
    """
    refs = sum(1 for c in chapters if REF_MARK in (c or ""))
    lines = ["## 本文の数値の点検", "",
             "★数値は「日付・数値の一覧」に集める決まり。"
             "本文に残っていれば二重管理になるので、ここで数えている。", "",
             "| 種類 | 残っている行数 |", "|---|---|",
             "| 日付 | %d |" % len(found["date"]),
             "| 条文番号 | %d |" % len(found["article"]),
             "| 文字起こしの時刻の写し込み | %d |" % len(found["timestamp"]),
             "| 章に付いた「一覧参照」 | %d / %d章 |" % (refs, len(chapters))]
    if found["total"] == 0:
        lines += ["", "★本文に日付・条文番号・時刻は残っていません。"]
    else:
        lines += ["", "★本文に数値が %d行 残っています（下記は先頭のみ）。"
                  % found["total"]]
        for k, label in (("date", "日付"), ("article", "条文番号"),
                         ("timestamp", "時刻")):
            for s in found[k][:3]:
                lines.append("- %s: %s" % (label, s))
    if not refs and chapters:
        lines += ["", "★どの章にも「一覧参照」がありません。"
                  "数値を外した章は、章だけを読むと「いつから」が分かりません。"]
    return "\n".join(lines)


#: ★レポート生成時に渡す「誤変換の対応表」。音声認識用の語彙とは役割が違う。
CORRECTION_FILES = ("vocab/corrections_ja.txt",)


def load_corrections(base_dir: Path, files=CORRECTION_FILES,
                     extra_path: str = "") -> list[str]:
    """★対応表を外のファイルから読む（コードに書かない・あとから足せる）。

    ★機械的に置換しない。文脈を見てモデルが直す材料として渡すだけ。
      「公権力」のように本当にその語である場合まで潰さないため。
    """
    rows: list[str] = []
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
            if t and not t.startswith("#") and "→" in t and t not in rows:
                rows.append(t)
    return rows


def filter_corrections_by_source(rows: list[str], source: str) -> tuple[list, list]:
    """★「直す前」が元の文字起こしに出てこない訂正を落とす。

    ★2026-08-21 実測: 対応表の29組すべてが訂正一覧に載ったが、うち5組
      （原稿法 / 欠陥自由 / 新衣工研 / 大理研扶養 / 私護事務）は
      ★文字起こしに1回も出ていなかった。モデルが対応表を書き写しただけ。
      直していないものを訂正として数えると、件数が嘘になる
      （「特定補助 → 特定補助」を落としたのと同じ形）。

    返す: (残した行, ★落とした行)。落とした分は呼び出し側が画面に出す。
    """
    keep, dropped = [], []
    src = source or ""
    for r in rows:
        left = r.split("→")[0]
        left = re.sub(r"（.*?）|\(.*?\)", "", left).strip()
        if left and left in src:
            keep.append(r)
        else:
            dropped.append(r)
    return keep, dropped


# ── 既にあるファイルから、研修DBへ入れる材料を作る ────────────────
#
#   ★2026-08-21 追加。それまで研修DBへ入れる口は
#     「アプリでその場で作った結果」にしか無く、
#     ★アプリを閉じると消えた（st.session_state に載っているだけ）。
#     手元に .md があっても入れられなかった。
#   ★足りないのは入口だけ。保存の中身（save_to_notion_kenshu）は既にある。
#     ここは★ファイルを読んで渡す材料を作るだけ。保存はしない。

#: ★足りなくても入れてよい。ただし★黙って空で保存しない（呼び出し側が出す）。
OPTIONAL_PARTS = ("transcript", "markmap_md", "summary_data")


def load_report_bundle(report_path, transcript_path="", markmap_path="",
                       summary_path="") -> dict:
    """★手元のファイルを読んで、研修DBへ渡す材料にする。

    ★無いものは無いまま返す（勝手に作らない）。
    ★読めなかったことを「空」と混ぜない。missing に理由つきで残す。
    返す: {title, report, summary, transcript, markmap_md, summary_data,
           missing:[...], source_info:[...]}
    """
    out = {"title": "", "report": "", "summary": "", "transcript": "",
           "markmap_md": "", "summary_data": None,
           "missing": [], "source_info": []}

    p = Path(report_path)
    try:
        out["report"] = p.read_text(encoding="utf-8")
        out["source_info"].append(p.name)
    except Exception as e:                                   # noqa: BLE001
        out["missing"].append("★レポート本体を読めません（%s）: %s"
                              % (p.name, type(e).__name__))
        return out

    for key, path, label in (("transcript", transcript_path, "文字起こし"),
                             ("markmap_md", markmap_path, "マインドマップ")):
        if not path:
            out["missing"].append("%sのファイルが選ばれていません" % label)
            continue
        try:
            out[key] = Path(path).read_text(encoding="utf-8")
            out["source_info"].append(Path(path).name)
        except Exception as e:                               # noqa: BLE001
            out["missing"].append("★%sを読めません（%s）: %s"
                                  % (label, Path(path).name, type(e).__name__))

    if summary_path:
        try:
            raw = Path(summary_path).read_text(encoding="utf-8")
            out["source_info"].append(Path(summary_path).name)
            if summary_path.lower().endswith(".json"):
                out["summary_data"] = json.loads(raw)
            else:
                # ★HTML はそのままでは構造化サマリーにならない。
                #   ★中身を作り直さない。無いものは無いままにする。
                out["missing"].append(
                    "要約は HTML なので、構造化サマリーとしては渡せません"
                    "（レポート本体には影響しません）")
        except Exception as e:                               # noqa: BLE001
            out["missing"].append("★要約を読めません（%s）: %s"
                                  % (Path(summary_path).name, type(e).__name__))
    else:
        out["missing"].append("要約のファイルが選ばれていません")

    # ★題名は「この記録について」の題名から取る（2026-08-21 変更）。
    #   ★それまで1行目の見出し（# 講演の記録）を拾っていたが、
    #     どのレポートも同じ見出しなので★研修DBで講演を見分けられなかった。
    #   ★取れなくても見出しには戻さない（title_from_report の中に理由あり）。
    out["title"], _t_miss = title_from_report(out["report"])
    out["missing"].extend(_t_miss)
    out["about"] = parse_about(out["report"])
    out["event_date"] = parse_event_date(out["about"].get("日時", ""))
    out["summary"] = "\n".join(out["report"].splitlines()[:10])
    return out


def bundle_readiness(bundle: dict) -> dict:
    """★入れる前に「何が足りないか」を出す。★足りなくても入れてよい。

    ★ここで良し悪しを決めない。決めるのは人。
    """
    rep = bundle.get("report") or ""
    # ★判定は shared-lib/gate_checks に1か所だけ置く。ここに写さない。
    try:
        import gate_checks as _G                             # noqa: PLC0415
        cov = _G.coverage_of(rep)
        marks = _G.mark_progress(rep)["total"]
    except Exception:                                        # noqa: BLE001
        cov, marks = None, None
    return {
        "レポート本体": "あり（%s字）" % f"{len(rep):,}" if rep else "★無し",
        "題名": bundle.get("title") or "★取れませんでした",
        # ★実施日は「講演が行われた日」。ページを作るときにしか入らないので、
        #   ★入らないことを押す前に見せる（あとから空だと気付けない）。
        #   2026-08-21: 実際に「入れた日」のまま1行残り、指摘されるまで
        #   気付けなかった（page 3c367ba3-…）。
        "実施日（講演の日）": (bundle.get("event_date")
                        or "★入りません（日時が読めません）"),
        "文字起こし": ("あり（%s字）" % f"{len(bundle['transcript']):,}"
                     if bundle.get("transcript") else "★無し"),
        "マインドマップ": "あり" if bundle.get("markmap_md") else "★無し",
        "構造化サマリー": "あり" if bundle.get("summary_data") else "★無し",
        "読んだ量の記載": ("%.1f%%" % (cov["ratio"] * 100) if cov
                       else "★書かれていません"),
        "公表状態の節": ("あり" if "★公的な裏付けが無い箇所" in rep else "★無し"),
        "要突合の印": marks if marks is not None else "★数えられません",
        "★足りないもの": bundle.get("missing") or [],
    }


# ── 「この記録について」から題名などを取り出す ──────────────────
#
#   ★2026-08-21 追加。それまで題名はレポートの1行目の見出し（# 講演の記録）を
#     拾っていた。全部のレポートが同じ見出しなので、★研修DBの題名が
#     どれも「講演の記録」になり、講演を見分けられなかった。
#   ★「この記録について」の節には 題名 / 日時 / 講師 / 主催 が書かれている。
#     ここから取る。★取れないときに見出しへ戻さない（また同じことが起きる）。

ABOUT_SECTION = "この記録について"
#: ★取り出す項目。研修DBの欄に入れられるかは呼び出し側が決める。
ABOUT_KEYS = ("題名", "日時", "講師", "主催", "配付資料")
#: ★モデルが「分からない」と書いたときの言い方。値として扱わない。
_UNKNOWN_WORDS = ("不明", "未確認", "記載なし", "（不明）", "-", "—")

_ABOUT_ROW = re.compile(r"^[-*・]\s*(題名|日時|講師|主催|配付資料)\s*[:：]\s*(.+?)\s*$", re.M)


def parse_about(report_text: str) -> dict:
    """★「この記録について」から項目を取り出す。

    ★「不明」と書かれていたら、値としては空にする（不明という文字を
      題名にしない）。取れなかったことは呼び出し側が扱う。
    """
    t = report_text or ""
    i = t.find(ABOUT_SECTION)
    if i < 0:
        return {}
    sec = t[i:]
    j = sec.find("\n## ", 3)
    if j > 0:
        sec = sec[:j]
    out = {}
    for m in _ABOUT_ROW.finditer(sec):
        k, v = m.group(1), m.group(2).strip()
        if v in _UNKNOWN_WORDS or not v:
            continue
        out[k] = v
    return out


#: ★和暦を西暦に直すための、元年の年（令和1年 = 2019年）。
_ERA = {"令和": 2018, "平成": 1988, "昭和": 1925}
_DATE = re.compile(r"(令和|平成|昭和)\s*([0-9０-９一二三四五六七八九十元]+)\s*年"
                   r"\s*([0-9０-９一二三四五六七八九十]+)\s*月"
                   r"\s*([0-9０-９一二三四五六七八九十]+)\s*日")


def parse_event_date(text: str) -> str | None:
    """★「令和8年4月22日」→ "2026-04-22"。読めなければ None（推測しない）。"""
    t = (text or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = _DATE.search(t)
    if not m:
        m2 = re.search(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", t)
        if not m2:
            return None
        y, mo, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return "%04d-%02d-%02d" % (y, mo, d)
    def num(s):
        return 1 if s == "元" else (int(s) if s.isdigit() else kanji_to_int(s))
    y = num(m.group(2))
    mo, d = num(m.group(3)), num(m.group(4))
    if None in (y, mo, d):
        return None
    return "%04d-%02d-%02d" % (_ERA[m.group(1)] + y, mo, d)


def title_from_report(report_text: str) -> tuple[str, list[str]]:
    """★研修DBに入れる題名を決める。返す: (題名, ★足りないもの)。

    順番:
      1. 「この記録について」の題名 … ★これが本命
      2. 講師名と日付から組み立てる … 題名が無いとき
      3. ★見出し（# 講演の記録）には戻さない
         理由: 全レポートが同じ見出しなので、題名にすると見分けられなくなる。
         2026-08-21 に実際に「講演の記録」で1行入り、次を入れると
         ★重複として止まる形になった。
    """
    about = parse_about(report_text)
    miss = []
    t = about.get("題名", "").strip()
    if t:
        return t[:200], miss

    miss.append("★レポートの「この記録について」に題名が書かれていません")
    # ★組み立てる。何から作ったかが分かる形にする。
    parts = []
    d = parse_event_date(about.get("日時", ""))
    if d:
        parts.append(d)
    if about.get("講師"):
        parts.append(about["講師"].split("（")[0].strip())
    if about.get("主催"):
        parts.append(about["主催"])
    if parts:
        made = "（題名なし）" + " ".join(parts)
        miss.append("題名の代わりに「%s」を使います" % made[:60])
        return made[:200], miss
    miss.append("★講師も日時も主催も取れないため、題名を作れません")
    return "", miss


# ── 添付資料の名前を、研修DBの「ファイル名」欄の形にする ────────────
#
#   ★2026-08-21 追加。それまでアプリは添付の名前を研修DBに渡していなかった。
#     今日の資料2本（20260422　研修資料.pdf / 次第　20260422.pdf）は
#     ★ファイル名欄も、ページ本文の「② 添付資料」も空のまま入った。
#   ★欄には名前だけを入れる。実体も道筋も入れない
#     （GDriveリンク欄に入れる URL は存在しない＝2026-08-21 実測）。
#   ★形は既存6行に揃える（2026-08-21 実測）:
#     - 複数は " / " で並べる … kindle_vol_01.pdf / kindle_vol_02.pdf / …
#     - 但し書きは括弧      … （調停委員用資料）/（資料・録音なし・口頭）

#: ★添付を「確かめた上で無い」と言い切るときの書き方。
NO_ATTACHMENT = "（添付なし）"
#: ★呼び出し側が渡してこなかったときの書き方。
#  ★空欄にしない。空欄だと「添付が無かった」と見分けが付かず、
#    渡し忘れに誰も気付けない（2026-08-21 に実際に起きた）。
ATTACHMENT_UNKNOWN = "★添付は未確認（渡されていません）"


def attachment_names(info: list | None) -> str:
    """★研修DBの「ファイル名」欄に入れる文字列。

    ★3つを区別する。空欄は返さない。
      名前あり … "a.pdf / b.pdf"
      空リスト … 確かめた上で添付が無い
      None    … ★呼び出し側が渡していない（渡し忘れ）
    """
    if info is None:
        return ATTACHMENT_UNKNOWN
    names = [str(f.get("name", "")).strip() for f in info if f.get("name")]
    if not names:
        return NO_ATTACHMENT
    return " / ".join(names)


def attachment_lines(info: list | None) -> list[str]:
    """★ページ本文「② 添付資料」に並べる行。名前と容量。

    ★欄と同じ判定を使う（別々に書くと片方だけ直る）。
    """
    if info is None:
        return [ATTACHMENT_UNKNOWN]
    out = []
    for f in info:
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        size = f.get("size")
        out.append("%s  (%s KB)" % (name, f"{int(size) // 1024:,}")
                   if isinstance(size, (int, float)) else name)
    return out or [NO_ATTACHMENT]
