# -*- coding: utf-8 -*-
r"""★全文を読むこと。読まなかったなら、そう書いてあること（2026-08-20 追加）。

★これは受け側検証（正本: 共有CLAUDE.md「★検査・テスト・監視を作るとき / 直すとき」）。
突き合わせ元: 「全文を読んだ」という申告（レポートに書かれる読了率）
突き合わせ先: ★区画の長さの合計（元テキストから機械的に数えた値）
  ★生成した側の言い分ではなく、分け方から数えた実数と突き合わせる。

■ なぜ要るか（★実測 2026-08-20）
  一定の長さを超えると compress_transcript() が
  先頭4000字 / 中間4000字 / 末尾4000字 の★3か所だけを抜き取っていた。
  2時間の講演（53,171字）では★77.4%が読まれず、
  24章のうち17章が読まれない区間にあり、
  ★最も価値のあった発言（00:14:58 のオフレコ）は欠落区間の中央にあった。
  ★欠落を告げる仕組みが無いので、出力は正常に見えた。

■ ★破綻点（先に挙げてから作った）
  区画のどれかが失敗しても、残りを連結すれば★見た目は正常なレポートになる。
  また黙って中身が落ちる形。
  検知: coverage() は★申告ではなく区画の長さから読んだ量を数え、
    coverage_section() がレポート本文に「読まれていない区間があります」と書く。
    下の test_一区画が失敗したら本文に欠落として出る が、
    ★1区画を落としたときに読了率が下がり、本文に欠落が出ることを確かめる。
"""
import ast
import io
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import report_builder as RB                                  # noqa: E402

APP = HERE / "voice_memo_app.py"
SRC = io.open(APP, encoding="utf-8").read()
TREE = ast.parse(SRC)

#: 2時間の講演に近い長さの合成データ（★外のファイルに頼らない）。
LONG = "\n".join("[%02d:%02d:%02d] これは%d行目の発言です。" % (
    i // 3600, i % 3600 // 60, i % 60, i) for i in range(1, 2200))


def _fake_results(chunks, fail=()):
    return [RB.ChunkResult(index=c.index, ok=(c.index not in fail),
                           text=("### 【章%d】\n本文" % c.index)
                           if c.index not in fail else "",
                           error="" if c.index not in fail else "わざと失敗")
            for c in chunks]


# ── ① ★長い入力でも全文が読まれること ────────────────────────

def test_区画の合計が元の長さと一致する():
    """★ここが崩れると、読んだ量の数えが嘘になる。"""
    assert len(LONG) > 50000, "★試験データが短すぎる（何も測れない）"
    chunks = RB.split_for_reading(LONG)
    assert sum(c.chars for c in chunks) == len(LONG)
    assert chunks[0].start == 0 and chunks[-1].end == len(LONG)


def test_区画に隙間も重なりも無い():
    chunks = RB.split_for_reading(LONG)
    for a, b in zip(chunks, chunks[1:]):
        assert a.end == b.start, "★%d と %d の間が繋がっていない" % (a.index, b.index)


def test_文脈は読んだ量に数えない():
    """★重なりを二重に数えて「全部読んだ」と申告しないこと。"""
    chunks = RB.split_for_reading(LONG)
    assert any(c.context for c in chunks[1:]), "★文脈が渡されていない"
    cov = RB.coverage(len(LONG), chunks, _fake_results(chunks))
    assert cov["read_chars"] == len(LONG), "★文脈まで数えている"
    assert cov["ratio"] == 1.0


def test_全区画が読めたら読了率は100パーセント():
    chunks = RB.split_for_reading(LONG)
    cov = RB.coverage(len(LONG), chunks, _fake_results(chunks))
    assert cov["complete"] is True
    assert cov["dropped_chars"] == 0


def test_実データでも全文が読まれる():
    """★合成でなく、実際の講演の文字起こしがあればそちらでも測る。"""
    real = Path(r"C:\Users\ohga\Downloads\transcript_2026-08-20 11_22_46.txt")
    if not real.exists():
        pytest.skip("実データが無い（合成データの試験は済んでいる）")
    t = real.read_text(encoding="utf-8")
    chunks = RB.split_for_reading(t)
    cov = RB.coverage(len(t), chunks, _fake_results(chunks))
    assert cov["read_chars"] == len(t) == 53171 or cov["ratio"] == 1.0
    assert cov["dropped_chars"] == 0


# ── ② ★抜き取りに戻すと落ちること（裏取り）──────────────────

def test_抜き取りに戻すと読了率が落ちる():
    """★これが 2026-08-20 まで実際に動いていた形。

    先頭・中間・末尾の3か所を4000字ずつ取る形を当てて、
    ★読了率が 100% を割ることを確かめる。割らなければこの検査は無意味。
    """
    third = len(LONG) // 3
    sampled = (LONG[:4000] + LONG[third:third + 4000] + LONG[-4000:])
    ratio = len(sampled) / len(LONG)
    assert ratio < 0.30, "★当て方の誤り（抜き取りになっていない）"
    # 抜き取った分しか読まなかった、という形に置き換えて数える
    chunks = RB.split_for_reading(LONG)
    ok = max(1, int(len(chunks) * ratio))
    cov = RB.coverage(len(LONG), chunks,
                      _fake_results(chunks, fail=range(ok, len(chunks))))
    assert cov["ratio"] < 1.0 and cov["complete"] is False
    assert "読まれていない区間があります" in RB.coverage_section(cov)


def test_抜き取りの関数が消えている():
    """★compress_transcript を戻したら落ちる。"""
    names = [n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)]
    assert "compress_transcript" not in names, (
        "★抜き取りの関数が復活しています。77.4% を黙って捨てていた実体です。")
    assert "generate_report_full" in names, "★全文読みの本体がありません"


def test_文字起こしを切る書き方が残っていない():
    """★transcript[:12000] のような固定の打ち切りが無いこと。"""
    bad = []
    for n in ast.walk(TREE):
        if not isinstance(n, ast.Subscript) or not isinstance(n.slice, ast.Slice):
            continue
        up = n.slice.upper
        tgt = getattr(n.value, "id", "")
        if (isinstance(up, ast.Constant) and isinstance(up.value, int)
                and up.value >= 1000
                and ("transcript" in tgt or "text" in tgt)):
            bad.append((n.lineno, tgt, up.value))
    assert not bad, "★文字起こしを黙って切っています: %s" % bad


# ── ③ ★読んだ量・捨てた量がレポートに書かれること ────────────

def test_レポートに読んだ量と捨てた量が入る():
    chunks = RB.split_for_reading(LONG)
    cov = RB.coverage(len(LONG), chunks, _fake_results(chunks))
    md = RB.assemble("題名", "全体像", ["### 【章】\n本文"], cov, [], "")
    assert "## 読んだ量と捨てた量" in md
    assert f"{len(LONG):,}" in md, "★入力の文字数が書かれていない"
    assert "★読んだ量" in md and "★捨てた量" in md


def test_一区画が失敗したら本文に欠落として出る():
    """★破綻点の検知そのもの。連結すると見た目は正常になる形を捕まえる。"""
    chunks = RB.split_for_reading(LONG)
    results = _fake_results(chunks, fail=(1,))
    cov = RB.coverage(len(LONG), chunks, results)
    md = RB.assemble("題名", "", [r.text for r in results if r.ok], cov, [], "")
    assert cov["complete"] is False
    assert "読まれていない区間があります" in md
    assert "区画1" in md, "★どの区間が欠けたかが書かれていない"


def test_出力上限で切れたら本文に出る():
    chunks = RB.split_for_reading(LONG)
    results = _fake_results(chunks)
    results[2].finish_reason = "length"
    cov = RB.coverage(len(LONG), chunks, results)
    md = RB.assemble("題名", "", [r.text for r in results], cov, [], "")
    assert cov["truncated"] == [2]
    assert "出力の上限で途中までしか書けていません" in md


def test_全部読めたときは読了と書く():
    chunks = RB.split_for_reading(LONG)
    cov = RB.coverage(len(LONG), chunks, _fake_results(chunks))
    assert "★全文を読んで作成しました" in RB.coverage_section(cov)


# ── ④ ★分野の語彙が渡されていること ──────────────────────

def test_語彙ファイルが読める():
    terms = RB.load_vocab(HERE)
    assert len(terms) >= 20, "★語彙が少なすぎる: %d" % len(terms)
    assert "成年後見制度" in terms and "事理弁識能力" in terms
    assert not any(t.startswith("#") for t in terms), "★注釈行を拾っている"


def test_語彙が上限に収まり入らない分を数えている():
    terms = RB.load_vocab(HERE)
    prompt, used, dropped = RB.whisper_prompt(terms)
    assert 0 < len(prompt) <= RB.VOCAB_PROMPT_MAX_CHARS
    assert used > 0 and used + dropped == len(terms), "★数が合わない"
    assert "成年後見制度" in prompt, "★上位の語が入っていない"


def test_音声認識に語彙を渡している():
    """★渡すだけ・費用は変わらないのに、渡していなかった。"""
    calls = [n for n in ast.walk(TREE)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "create"
             and "transcriptions" in ast.dump(n.func)]
    assert calls, "★音声認識の呼び出しが見つからない"
    for c in calls:
        kw = {k.arg for k in c.keywords}
        assert "prompt" in kw, "★%d行目: 語彙ヒントを渡していない" % c.lineno


def test_語彙をコードに直接書いていない():
    """★あとから足せる形であること（ファイルから読む）。"""
    assert "vocab/legal_ja.txt" in io.open(
        HERE / "report_builder.py", encoding="utf-8").read()
    assert (HERE / "vocab" / "legal_ja.txt").is_file()


# ── ⑤ ★時刻が取れること ────────────────────────────────

def test_時刻付きで受け取っている():
    calls = [n for n in ast.walk(TREE)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "create"
             and "transcriptions" in ast.dump(n.func)]
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert "response_format" in kw, "★%d行目: 時刻を受け取っていない" % c.lineno
        assert getattr(kw["response_format"], "value", "") == "verbose_json"


def test_時刻から本文が組み立てられる():
    segs = [{"start": 0, "end": 3, "text": "あ"},
            {"start": 3, "end": 6, "text": "い"}]
    t = RB.timestamped_text(segs)
    assert t.startswith("[00:00:00] あ") and "[00:00:03] い" in t


def test_時刻から空白区間が拾える():
    segs = [{"start": 0, "end": 5, "text": "あ"},
            {"start": 40, "end": 45, "text": "い"},
            {"start": 46, "end": 50, "text": "う"}]
    gaps = RB.silent_gaps(segs, min_sec=8)
    assert len(gaps) == 1
    assert gaps[0]["from_hhmmss"] == "00:00:05" and gaps[0]["sec"] == 35
    assert "00:00:05" in RB.gaps_section(gaps)


def test_空白が無ければ無いと書く():
    assert "なし" in RB.gaps_section([])


# ── ⑥ ★費用の関所（抜き取りに倒れないこと）────────────────

def test_見込みが上限を超えたら例外で止まる():
    e = RB.estimate_plan_jpy(total_chars=5_000_000, material_chars=3000,
                             prompt_chars=1500, n_chunks=1200,
                             out_chars_per_chunk=2600)
    assert e["over_limit"] is True
    assert e["jpy"] > RB.COST_LIMIT_JPY


def test_今日の講演は上限に収まる():
    e = RB.estimate_plan_jpy(total_chars=53171, material_chars=2953,
                             prompt_chars=1500, n_chunks=13,
                             out_chars_per_chunk=2600)
    assert e["over_limit"] is False
    assert e["jpy"] < RB.COST_LIMIT_JPY
    assert e["is_estimate"] is True, "★見積りであることが分かる形になっていない"


def test_上限を超えたときに抜き取りへ倒れない():
    """★止まるときに「一部だけ読む」に戻らないこと。"""
    assert "CostLimitExceeded" in SRC
    i = SRC.index("except RB.CostLimitExceeded")
    blk = SRC[i:i + 900]
    assert "st.stop()" in blk, "★止まっていない"
    for w in ("compress", "[:12000]", "sampled", "抜き取"):
        assert w not in blk, "★止まるときに抜き取りへ倒れています: %s" % w


def test_見込み額の明細に前提が出る():
    e = RB.estimate_plan_jpy(53171, 2953, 1500, 13, 2600)
    note = RB.cost_note(e)
    for w in ("見込み", "上限", "単価", "ドル円", "見積り"):
        assert w in note, "★%s が書かれていない" % w
