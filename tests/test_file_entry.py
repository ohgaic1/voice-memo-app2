# -*- coding: utf-8 -*-
r"""★手元のファイルから研修DBに入れる口（2026-08-21 追加）。

★これは受け側検証（正本: 共有CLAUDE.md「★検査・テスト・監視を作るとき / 直すとき」）。
突き合わせ元: 「ファイルの中身を渡した」という思い込み
突き合わせ先: ★実際にファイルを読んで組み立てた材料の中身
  ＋ ★両方の入口が save_to_notion_kenshu に渡す引数の集合（実物の構文）

■ なぜ要るか（★実測 2026-08-21）
  それまで研修DBへ入れる口は「アプリでその場で作った結果」にしか無く、
  ★アプリを閉じると消えた（st.session_state に載っているだけ）。
  手元に .md があっても入れられなかった。

■ ★破綻点（先に挙げてから作った）
  入口が2つになると、★片方だけ直る。
  （アプリで作った直後の保存と、ファイルから入れる保存で渡すものが食い違う）
  検知: 両方が★同じ save_to_notion_kenshu を呼び、渡す引数の集合が
    ★同じであることを ast で固定する。片方に足して他方に足さないと落ちる。
"""
import ast
import io
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, r"C:\dev\shared-lib")

import report_builder as RB                                  # noqa: E402

SRC = io.open(HERE / "voice_memo_app.py", encoding="utf-8").read()
TREE = ast.parse(SRC)


# ── ① ★入口が2つでも、渡すものが食い違わないこと ────────────

def _save_calls():
    return [c for c in ast.walk(TREE)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "save_to_notion_kenshu"]


def test_保存の中身を作り直していない():
    """★足りないのは入口だけ。保存の関数は1つ。"""
    defs = [n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)]
    assert defs.count("save_to_notion_kenshu") == 1, "★保存が2つあります"


def test_入口が2つあること():
    assert len(_save_calls()) == 2, "★入口の数が変わっています"


def test_両方の入口が同じ引数で保存する():
    """★破綻点の検知。片方に足して他方に足さないと落ちる。"""
    sets = [frozenset(k.arg for k in c.keywords) for c in _save_calls()]
    assert len(set(sets)) == 1, (
        "★入口によって渡すものが違います: %s"
        % [sorted(s) for s in sets])
    assert "transcript" in sets[0] and "markmap_md" in sets[0]


def test_引数を片方だけ減らすと落ちる():
    """★仕掛けの裏取り。"""
    a, b = [frozenset(k.arg for k in c.keywords) for c in _save_calls()]
    assert a == b
    assert (a - {"transcript"}) != b, "★変異が効いていない"


# ── ② ★ファイルから読んだ内容が欠けずに渡ること ──────────────

def _write(tmp, name, text):
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


REPORT = """# 講演の記録

## 1. この記録について

- 題名: テスト講演の記録
- 講師: テスト講師

## 3. 本編
### 【章】本文

## ★公的な裏付けが無い箇所

★1件。
| 主題・発言・時刻 |
|---|
| 章 | 「オフレコ」 | 00:14:57 |

## 読んだ量と捨てた量
| 入力（文字起こし全体） | 1,000字 |
| ★読んだ量 | 1,000字（100.0%） |
"""


def test_ファイルから読んだ内容が欠けずに渡る(tmp_path):
    rep = _write(tmp_path, "r.md", REPORT)
    tr = _write(tmp_path, "t.txt", "[00:00:00] 文字起こしの中身")
    mm = _write(tmp_path, "m.md", "# マップ\n## 章")
    b = RB.load_report_bundle(rep, tr, mm)
    assert b["report"] == REPORT, "★レポートが欠けています"
    assert b["transcript"] == "[00:00:00] 文字起こしの中身"
    assert b["markmap_md"] == "# マップ\n## 章"
    # ★題名は見出し（講演の記録）ではなく「この記録について」から取る
    #   （2026-08-21 変更。詳細は tests/test_title_from_about.py）
    assert b["title"] == "テスト講演の記録", b["title"]
    assert b["summary"], "★概要が空です"
    assert "r.md" in b["source_info"] and "t.txt" in b["source_info"]


def test_無いものは無いまま渡す(tmp_path):
    """★勝手に作らない。"""
    b = RB.load_report_bundle(_write(tmp_path, "r.md", REPORT))
    assert b["transcript"] == "" and b["markmap_md"] == ""
    assert b["summary_data"] is None
    assert any("文字起こし" in m for m in b["missing"])


def test_読めないことを空と混ぜない(tmp_path):
    b = RB.load_report_bundle(_write(tmp_path, "r.md", REPORT),
                              transcript_path=str(tmp_path / "無い.txt"))
    assert any("★文字起こしを読めません" in m for m in b["missing"]), b["missing"]


def test_レポートを読めなければそこで止まる(tmp_path):
    b = RB.load_report_bundle(str(tmp_path / "無い.md"))
    assert b["report"] == ""
    assert any("★レポート本体を読めません" in m for m in b["missing"])


# ── ③ ★足りないものが押す前に分かること ────────────────────

def test_足りないものが出る(tmp_path):
    b = RB.load_report_bundle(_write(tmp_path, "r.md", REPORT))
    r = RB.bundle_readiness(b)
    assert r["文字起こし"] == "★無し"
    assert r["構造化サマリー"] == "★無し"
    assert r["★足りないもの"], "★足りないものが出ていない"


def test_足りていれば足りていると出る(tmp_path):
    b = RB.load_report_bundle(_write(tmp_path, "r.md", REPORT),
                              _write(tmp_path, "t.txt", "文字起こし"),
                              _write(tmp_path, "m.md", "# マップ"))
    r = RB.bundle_readiness(b)
    assert r["文字起こし"].startswith("あり")
    assert r["マインドマップ"] == "あり"


def test_読んだ量と公表状態の有無が出る(tmp_path):
    r = RB.bundle_readiness(RB.load_report_bundle(_write(tmp_path, "r.md", REPORT)))
    assert r["読んだ量の記載"] == "100.0%"
    assert r["公表状態の節"] == "あり"


def test_読んだ量が無ければ無いと出る(tmp_path):
    r = RB.bundle_readiness(RB.load_report_bundle(
        _write(tmp_path, "r.md", "# 題名\n本文だけ")))
    assert r["読んだ量の記載"] == "★書かれていません"
    assert r["公表状態の節"] == "★無し"


def test_題名が取れなければ足りないに入る(tmp_path):
    b = RB.load_report_bundle(_write(tmp_path, "r.md",  "# 講演の記録\n本文だけ"))
    assert b["title"] == ""
    assert any("題名" in m for m in b["missing"])


def test_黙って空で保存しない():
    """★足りないものを画面に出す実装があること。"""
    i = SRC.index("bundle_readiness")
    blk = SRC[i:i + 1200]
    assert "★足りないもの" in blk and "st.warning" in blk, (
        "★足りないものを画面に出していません")
    assert "足りなくても入れられます" in blk, (
        "★入れてよいことが書かれていません（勝手に止めない）")


# ── ④ ★二度入れられないようにする ──────────────────────

def test_同じ題名の行を探す実装がある():
    defs = [n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)]
    assert "find_existing_kenshu" in defs


def test_見つかったら画面で止める():
    i = SRC.index("find_existing_kenshu(_bundle")
    blk = SRC[i:i + 900]
    assert "すでにあります" in blk, "★重複を知らせていない"
    assert "disabled=" in blk, "★押せないようにしていない"
    assert "★同じ記録があっても入れる" in blk, (
        "★どうしても入れたいときの道が無い（人が決められない）")


def test_見に行けなかったことを無かったと混ぜない():
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "find_existing_kenshu")
    body = ast.unparse(fn)
    assert "★確かめられません" in body, (
        "★見に行けなかったのに『無かった』として通しています")


# ── ⑤ ★入れたあと、読み直して確かめること ──────────────────

def test_読み直して確かめる実装がある():
    defs = [n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)]
    assert "verify_saved_kenshu" in defs
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "verify_saved_kenshu")
    body = ast.unparse(fn)
    assert "blocks" in body and "children" in body, "★本文を読み直していない"
    assert "★欠け" in body


def test_送った側の戻り値だけを根拠にしない():
    i = SRC.index("if not _saved:")
    blk = SRC[i:i + 1100]
    assert "verify_saved_kenshu" in blk, "★読み直していない"
    assert "確かめられませんでした" in blk


def test_投入可を付けていない():
    """★人が押すもの。ここでは付けない。"""
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "save_to_notion_kenshu")
    assert "投入可" not in ast.unparse(fn), "★人の印を機械が付けています"
    i = SRC.index("save_from_file")
    assert "投入可" not in SRC[i:i + 2000] or "チェックは付いていません" in SRC[i:i + 2000]


# ── ⑥ ★本文を打ち切らないこと（2026-08-21 実測で捕まえた）────────

def test_レポート本文を打ち切らない():
    """★実測 2026-08-21: report_blocks[:200] で切っており、
    709ブロック中★509ブロック・12,886字（本文の70%）が黙って捨てられていた。
    ★読み直しの検査（verify_saved_kenshu）がこれを捕まえた。
    """
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "save_to_notion_kenshu")
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice)):
            continue
        up = n.slice.upper
        tgt = ast.unparse(n)
        if (isinstance(up, ast.Constant) and isinstance(up.value, int)
                and up.value >= 50
                and any(w in tgt for w in ("blocks", "chunks", "transcript",
                                           "report"))):
            pytest.fail("★%d行で本文を切っています: %s" % (n.lineno, tgt[:60]))


def test_文字起こしも打ち切らない():
    """★あとで引き直すときの一次資料。切らない。"""
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "save_to_notion_kenshu")
    body = ast.unparse(fn)
    assert "min(len(transcript), 60000)" not in body, "★60000字で切っています"
    assert "tr_chunks[:90]" not in body, "★90ブロックで切っています"


def test_打ち切りを戻すと検査が落ちる():
    """★仕掛けの裏取り。"""
    broken = SRC.replace("_append_blocks(page_id, report_blocks, headers)",
                         "_append_blocks(page_id, report_blocks[:200], headers)")
    t = ast.parse(broken)
    fn = next(n for n in ast.walk(t)
              if isinstance(n, ast.FunctionDef) and n.name == "save_to_notion_kenshu")
    hits = [ast.unparse(n) for n in ast.walk(fn)
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice)
            and isinstance(n.slice.upper, ast.Constant)
            and "report_blocks" in ast.unparse(n)]
    assert hits, "★変異が効いていない"
