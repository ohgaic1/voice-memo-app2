# -*- coding: utf-8 -*-
r"""★添付資料の名前を研修DBに渡す（2026-08-21 追加）。

★これは受け側検証（正本: 共有CLAUDE.md「★検査・テスト・監視を作るとき / 直すとき」）。
突き合わせ元: アプリが受け取った添付ファイルの名前と容量（attachment_file_info）
突き合わせ先: ★save_to_notion_kenshu が Notion に渡す
  properties["ファイル名"] と、ページ本文「② 添付資料」のブロック（実物の構文）

■ なぜ要るか（★実測 2026-08-21）
  今日のレポートを作るとき、配付資料2本
  （20260422　研修資料.pdf / 次第　20260422.pdf）を渡していた。
  ★それが研修DBのどこにも入っていなかった。
  ファイル名欄は空、ページ本文の「② 添付資料」は（なし）。
  ファイルからの入口が attachment_file_info=None を渡していたため。

■ ★破綻点（先に挙げてから作った）
  入口が3つ目になったとき、あるいは片方が渡すのをやめたとき、
  ★ファイル名欄は空になる。そして「空」は「添付が無かった」と
  ★見分けが付かない。今日の1行がまさにそれで、誰も気付けなかった。
  検知: ①3値で分ける（名前あり／確かめた上で無し／★渡されていない）。
          空欄は作らない
        ②両方の入口が attachment_file_info を渡すことを ast で固定し、
          ★片方だけ渡す形に戻すと落ちる
"""
import ast
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, r"C:\dev\shared-lib")

import report_builder as RB                                  # noqa: E402

SRC = io.open(HERE / "voice_memo_app.py", encoding="utf-8").read()
TREE = ast.parse(SRC)

TWO = [{"name": "20260422　研修資料.pdf", "size": 2101806},
       {"name": "次第　20260422.pdf", "size": 323380}]


# ── ① ★既存6行の形に揃っていること ──────────────────────

def test_複数はスラッシュ区切り():
    """★2026-08-21 実測の既存行に揃える:
    kindle_vol_01.pdf / kindle_vol_02.pdf / kindle_vol_03.pdf
    """
    assert RB.attachment_names(TWO) == \
        "20260422　研修資料.pdf / 次第　20260422.pdf"


def test_名前だけで実体も道筋も入れない():
    got = RB.attachment_names(TWO)
    assert "\\\\" not in got and "http" not in got and "C:" not in got, got


def test_容量は欄に入れず本文にだけ入れる():
    assert "KB" not in RB.attachment_names(TWO)
    lines = RB.attachment_lines(TWO)
    assert lines[0].startswith("20260422　研修資料.pdf") and "KB" in lines[0]
    assert "2,052 KB" in lines[0], lines[0]


# ── ② ★空と渡し忘れを混ぜないこと ─────────────────────

def test_確かめた上で無いときは添付なしと書く():
    assert RB.attachment_names([]) == "（添付なし）"
    assert RB.attachment_lines([]) == ["（添付なし）"]


def test_渡されていないときは未確認と書く():
    """★空欄にしない。空欄だと添付が無かったと見分けが付かない。"""
    assert RB.attachment_names(None) == "★添付は未確認（渡されていません）"
    assert RB.attachment_lines(None) == ["★添付は未確認（渡されていません）"]


def test_空と渡し忘れが別の文字列になる():
    """★これが同じになったら、今日と同じ見落としが起きる。"""
    assert RB.attachment_names([]) != RB.attachment_names(None)


def test_名前の無い項目は数えない():
    assert RB.attachment_names([{"size": 10}]) == "（添付なし）"


def test_容量が無くても名前は出る():
    assert RB.attachment_lines([{"name": "a.pdf"}]) == ["a.pdf"]


# ── ③ ★両方の入口が渡すこと（片方だけに戻すと落ちる）────────

def _save_calls():
    return [c for c in ast.walk(TREE)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "save_to_notion_kenshu"]


def test_入口は2つのまま():
    assert len(_save_calls()) == 2


def test_両方の入口が添付を渡す():
    for c in _save_calls():
        kw = {k.arg: k.value for k in c.keywords}
        assert "attachment_file_info" in kw, "★片方が渡していません"


def test_片方だけNoneに戻すと落ちる():
    """★仕掛けの裏取り。2026-08-21 まで実際にこの形だった。"""
    broken = SRC.replace("attachment_file_info=_att_info,",
                         "attachment_file_info=None,")
    assert broken != SRC, "★変異が当たっていない"
    t = ast.parse(broken)
    bad = []
    for c in ast.walk(t):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) \
                and c.func.id == "save_to_notion_kenshu":
            for k in c.keywords:
                if k.arg == "attachment_file_info" \
                        and isinstance(k.value, ast.Constant) \
                        and k.value.value is None:
                    bad.append(c.lineno)
    assert bad, "★変異が効いていない（None を渡しても捕まらない）"


def test_渡していないことを添付なしに化けさせない():
    """★result.get(..., []) に戻すと、渡し忘れが「添付なし」に化ける。"""
    call = [c for c in _save_calls()
            if any(k.arg == "attachment_file_info"
                   and isinstance(k.value, ast.Call) for k in c.keywords)]
    assert call, "★アプリ側の入口が見つかりません"
    kw = next(k for k in call[0].keywords if k.arg == "attachment_file_info")
    assert len(kw.value.args) == 1, (
        "★既定値を付けています（渡し忘れが添付なしに化けます）")


# ── ④ ★欄と本文が同じ判定を使うこと ────────────────────

def _save_fn():
    return next(n for n in ast.walk(TREE)
                if isinstance(n, ast.FunctionDef)
                and n.name == "save_to_notion_kenshu")


def test_欄と本文が同じ判定を使う():
    body = ast.unparse(_save_fn())
    assert "RB.attachment_names(attachment_file_info)" in body, "★欄に入れていません"
    assert "RB.attachment_lines(attachment_file_info)" in body, "★本文が別実装です"
    assert "'（なし）'" not in body, "★古い書き方が残っています"


def test_GDriveリンクを触っていない():
    """★入れる URL が存在しない（2026-08-21 実測 9/10行が空）。"""
    body = ast.unparse(_save_fn())
    assert "GDriveリンク" not in body, "★GDriveリンク欄に書こうとしています"


def test_実体を上げる処理を作っていない():
    """★名前だけ。置き場は作らない。"""
    fn = ast.unparse(_save_fn())
    for w in ("rclone", "drive.files", "files().create", "upload_to_drive"):
        assert w not in fn, "★実体を上げようとしています: %s" % w
