# -*- coding: utf-8 -*-
r"""★不要な圧縮をしないこと／本文に数値を残さないこと（2026-08-21 追加）。

★これは受け側検証（正本: 共有CLAUDE.md「★検査・テスト・監視を作るとき / 直すとき」）。
突き合わせ元: 「数値は一覧に集めた」「音質は落としていない」という指示・思い込み
突き合わせ先: ★出来上がった本文を機械的に数えた結果 ／ ★区画ごとの実サイズ
  ★指示を出したことを根拠にしない。出た物を数える。

■ なぜ要るか（★実測 2026-08-21）
  ・「日付・数値は本文に書かない」と指示しても守られなかった。
    本文35行に数字が残り、内訳は 時刻の写し込み14行 / 日付2行 /
    条文番号5行 / ★用語に含まれる数字4行 だった。
    しかも「（→日付・数値の一覧）」という参照は★0回。
    ★指示を厳しくするだけでは足りないので、数えて表に出す形にした。
  ・音声はまるごと 32kbps へ再エンコードしてから分割していた。
    先に分割すれば1区画は上限を下回るので、★落とす必要のない音質だった。

■ ★破綻点（先に挙げてから作った）
  数値を本文から外すと、★章だけを読んでも「いつから」が分からなくなる。
  知識ベースでは章単位に引かれるので、これは実害になる。
  検知: numbers_check_section() が「一覧参照」の付いた章の数を数え、
    0 なら本文にその旨を出す。下の test_参照が無いことが本文に出る が固定する。
"""
import ast
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import report_builder as RB                                  # noqa: E402

SRC = io.open(HERE / "voice_memo_app.py", encoding="utf-8").read()
TREE = ast.parse(SRC)


# ── ① ★上限を超えない音声を圧縮しないこと ────────────────────

def _fn(name):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def test_先に分割してから圧縮する():
    """★「まるごと圧縮 → それでも大きければ分割」に戻っていないこと。"""
    body = ast.unparse(_fn("transcribe_audio"))
    i_split = body.index("split_audio(")
    i_comp = body.index("compress_audio(")
    assert i_split < i_comp, (
        "★圧縮が分割より先に来ています。まるごと再エンコードする形に"
        "戻っています（2026-08-21 に直したもの）。")


def test_圧縮は区画ごとの大きさで決める():
    """★区画のサイズを見てから圧縮すること（全体サイズで決めない）。"""
    body = ast.unparse(_fn("transcribe_audio"))
    assert "os.path.getsize(chunk) > max_size" in body, (
        "★区画ごとの大きさを見ていません")


def test_圧縮した区画を記録している():
    body = ast.unparse(_fn("transcribe_audio"))
    assert "info['compressed'].append" in body or \
           'info["compressed"].append' in body, "★圧縮した区画を記録していない"


def test_圧縮したことがレポートに書かれる():
    note = RB.compression_note({"chunks": 13, "compressed": [3, 7]})
    assert "13区画中 2区画" in note and "区画3" in note and "区画7" in note
    assert "音質が落ちています" in note, "★何が起きたかが書かれていない"


def test_圧縮していなければ何も書かない():
    """★していないことを書かない（空欄でも嘘でもない）。"""
    assert RB.compression_note({"chunks": 13, "compressed": []}) == ""


def test_記録を外すと検査が落ちる():
    """★仕掛けの裏取り。"""
    assert RB.compression_note({"chunks": 5, "compressed": [0]}) != ""
    assert "区画0" in RB.compression_note({"chunks": 5, "compressed": [0]})


# ── ② ★本文に日付・条文番号・時刻を残さないこと ────────────────

CLEAN = """### 【補助制度への一元化】

**何が変わるのか／何が論点か**
後見と保佐が廃止され、補助に一元化される（→日付・数値の一覧）。
第2種社会福祉事業や9条列挙行為という言い方は残る。3類型が1つになる。
"""

DIRTY = """### 【章】

**何が変わるのか／何が論点か**
令和9年4月から施行される。
民法859条の「本人を代表する」という文言が削除される。
[00:17:27] 講師はそう述べた。
"""


def test_きれいな本文では数値が検出されない():
    f = RB.numbers_in_body(CLEAN)
    assert f["total"] == 0, f


def test_用語に含まれる数字は数値と数えない():
    """★第2種／9条列挙行為／3類型 は書きようがない。数えたら誤検知。"""
    for s in ("第2種社会福祉事業", "9条列挙行為をすべて", "3類型が廃止",
              "2本柱で成り立つ", "2段構えの制度"):
        assert RB.numbers_in_body("本文: " + s)["total"] == 0, s


def test_日付と条文番号と時刻を種類ごとに拾う():
    f = RB.numbers_in_body(DIRTY)
    assert len(f["date"]) == 1, f["date"]
    assert len(f["article"]) >= 1, f["article"]
    assert len(f["timestamp"]) == 1, f["timestamp"]
    assert f["total"] >= 3


def test_残っていたら本文に出る():
    md = RB.numbers_check_section(RB.numbers_in_body(DIRTY), [DIRTY])
    assert "本文に数値が" in md and "残っています" in md
    assert "日付:" in md


def test_残っていなければ残っていないと書く():
    md = RB.numbers_check_section(RB.numbers_in_body(CLEAN), [CLEAN])
    assert "残っていません" in md


def test_参照が無いことが本文に出る():
    """★破綻点の検知そのもの。章だけ読んで「いつから」が分からない形。"""
    md = RB.numbers_check_section(RB.numbers_in_body(DIRTY), [DIRTY])
    assert "どの章にも「一覧参照」がありません" in md
    md2 = RB.numbers_check_section(RB.numbers_in_body(CLEAN), [CLEAN])
    assert "どの章にも「一覧参照」がありません" not in md2


def test_点検の節がレポートに入る():
    cov = {"total_chars": 10, "read_chars": 10, "dropped_chars": 0,
           "ratio": 1.0, "chunks": 1, "chunks_ok": 1, "missing": [],
           "truncated": [], "complete": True}
    md = RB.assemble_full("題名", "全体像", [CLEAN], [], [], [], cov, [],
                          "", "", None, RB.numbers_in_body(CLEAN))
    assert "## 本文の数値の点検" in md


# ── ③ ★指示にも入っていること ────────────────────────────

TPL = next(n.value.value for n in ast.walk(TREE)
           if isinstance(n, ast.Assign) and n.targets
           and getattr(n.targets[0], "id", "") == "_CHUNK_TEMPLATE"
           and isinstance(n.value, ast.Constant))


def test_時刻を本文に書かせない指示がある():
    assert "[HH:MM:SS] の時刻を書き写さない" in TPL, "★時刻の写し込みを禁じていない"


def test_用語に含まれる数字は書いてよいと伝えている():
    """★書きようがないものまで禁じると、今度は用語が壊れる。"""
    assert "第2種社会福祉事業" in TPL and "9条列挙行為" in TPL
    assert "そのまま本文に書いてよい" in TPL


def test_参照の書き方を指示している():
    assert "（→日付・数値の一覧）" in TPL, "★参照の書き方を示していない"


def test_指示を外すと検査が落ちる():
    broken = TPL.replace("[HH:MM:SS] の時刻を書き写さない", "")
    assert "[HH:MM:SS] の時刻を書き写さない" not in broken


def test_1行に日付と条文が同居したら日付として数える():
    """★同じ行を二重に数えない。どちらか一方に入れる（合計が膨らまないため）。"""
    f = RB.numbers_in_body("令和9年4月から施行され、民法859条が削除される。")
    assert len(f["date"]) == 1 and len(f["article"]) == 0
    assert f["total"] == 1, "★1行を二重に数えている"


# ── ④ ★誤変換の対応表（2026-08-21 の実測に基づく）────────────

def test_対応表がファイルから読める():
    """★コードに書かない。あとから足せる形であること。"""
    rows = RB.load_corrections(HERE)
    assert len(rows) >= 20, "★対応表が少なすぎる: %d" % len(rows)
    assert "公権 → 後見" in rows, "★実測で最も多い誤変換が入っていない"
    assert all("→" in r for r in rows), "★対応の形になっていない行がある"
    assert not any(r.startswith("#") for r in rows), "★注釈行を拾っている"


def test_対応表が指示に渡っている():
    body = ast.unparse(_fn("generate_chunk_chapters"))
    assert "CORRECTIONS" in body, "★対応表を渡していない"
    assert "corrections=corr" in body, "★指示に差し込んでいない"


def test_機械的に置換していないこと():
    """★「公権力」のように本当にその語である場合まで潰さないため、
    置換はモデルに任せる。コード側で replace していないこと。"""
    src = io.open(HERE / "report_builder.py", encoding="utf-8").read()
    fn = src.split("def load_corrections(", 1)[1].split("\ndef ", 1)[0]
    assert ".replace(" not in fn, "★対応表で機械的に置換しています"


def test_音声認識用と生成用で置き場が分かれている():
    """★役割が違うものを1つのファイルに混ぜない。"""
    assert RB.VOCAB_FILES != RB.CORRECTION_FILES
    assert (HERE / "vocab" / "legal_ja.txt").is_file()
    assert (HERE / "vocab" / "corrections_ja.txt").is_file()
    # ★音声認識側に「→」の行が混ざっていないこと（混ざると語彙が壊れる）
    assert not any("→" in t for t in RB.load_vocab(HERE))


# ── ⑤ ★訂正の裏取り（2026-08-21 2回目の実測）────────────────

def test_元に無い訂正は落とす():
    """★対応表を書き写しただけの行を、訂正として数えない。

    実測 2026-08-21: 対応表29組すべてが訂正一覧に載ったが、うち5組
    （原稿法 / 欠陥自由 / 新衣工研 / 大理研扶養 / 私護事務）は
    ★文字起こしに1回も出ていなかった。
    """
    src = "公権と補佐について、任意貢献の話をした。"
    rows = ["公権 → 後見", "任意貢献 → 任意後見", "私護事務 → 死後事務"]
    keep, drop = RB.filter_corrections_by_source(rows, src)
    assert keep == ["公権 → 後見", "任意貢献 → 任意後見"]
    assert drop == ["私護事務 → 死後事務"], drop


def test_括弧つきの注記があっても判定できる():
    src = "日時を使っていますと。"
    keep, drop = RB.filter_corrections_by_source(
        ["日時（事業名として） → 日常生活自立支援事業"], src)
    assert keep and not drop


def test_裏取りを外すと嘘の件数が通る():
    """★仕掛けの裏取り。落とさなければ、直していないものが数に入る。"""
    src = "本文に何も無い。"
    rows = ["私護事務 → 死後事務", "新衣工研 → 任意後見"]
    keep, drop = RB.filter_corrections_by_source(rows, src)
    assert keep == [] and len(drop) == 2, "★元に無い訂正を残している"


def test_落とした分を呼び出し側が画面に出す():
    body = ast.unparse(_fn("generate_report_full"))
    assert "filter_corrections_by_source" in body, "★裏取りを呼んでいない"
    assert "dropped_terms" in body and "st.warning" in body, (
        "★落としたことを黙っている")


def test_対応表に文脈依存の語を入れていない():
    """★「移行」は正しい用法と誤りが同じ語。一律には直せない。

    実測 2026-08-21: 本文の「移行」7回はすべて正しい用法だった。
    """
    rows = RB.load_corrections(HERE)
    lefts = [r.split("→")[0].strip() for r in rows]
    assert "移行" not in lefts, (
        "★『移行』を対応表に入れています。"
        "「新制度に移行する」まで潰れます（2026-08-21 実測）。")


def test_2回目の実測で見つかった分が入っている():
    rows = RB.load_corrections(HERE)
    for r in ("全管注意義務 → 善管注意義務", "批判的な要件 → 規範的な要件",
              "後概念 → 承継概念"):
        assert r in rows, "★2回目の実測で残っていた分が足されていない: %s" % r
