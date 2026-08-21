# -*- coding: utf-8 -*-
r"""★区画ごとの指示と、まとめの節が揃っていること（2026-08-20 追加）。

★これは受け側検証（正本: 共有CLAUDE.md「★検査・テスト・監視を作るとき / 直すとき」）。
突き合わせ元: 「こういう指示を出している」という思い込み
突き合わせ先: ★voice_memo_app.py の中の指示文そのもの（実物の文字列）
  ＋ ★report_builder が実際に組み上げた本文（実際に呼んで出力を見る）

■ なぜ要るか
  区画ごとに分けて読む形にしたので、指示文もその形に合わせて入れ替えた。
  ★指示が1つ抜けても出力はそれらしく見える（章が並んでいるだけで通る）。
  抜けたことに気づく手立てが要る。

■ ★破綻点（先に挙げてから作った）
  ある区画の出力が★章の形から外れても（見出しの無い塊が返っても）、
  連結すれば見た目は通る。読了率は 100% のままなので気づけない。
  検知: chapters_missing_heading() が見出しの無い区画を拾い、
    ★画面とレポート本文の両方に出す。下の
    test_見出しの無い区画が検知される / test_見出しが無いことが本文に出る が固定する。
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


def _const(name: str) -> str:
    """★ast で定数の中身を取り出す（実物を見る）。"""
    for n in ast.walk(TREE):
        if (isinstance(n, ast.Assign) and n.targets
                and getattr(n.targets[0], "id", "") == name):
            v = n.value
            if isinstance(v, ast.Constant):
                return v.value
            if isinstance(v, ast.JoinedStr) or isinstance(v, ast.BinOp):
                return ast.unparse(v)
    raise AssertionError("★%s が見つからない" % name)


TPL = _const("_CHUNK_TEMPLATE")
SYS = ast.unparse(next(n.value for n in ast.walk(TREE)
                       if isinstance(n, ast.Assign) and n.targets
                       and getattr(n.targets[0], "id", "") == "_CHUNK_SYSTEM"))


# ── ① ★区画ごとの指示に、決めた項目が入っていること ──────────────

REQUIRED_HEADINGS = [
    "### 【主題】",
    "**何が変わるのか／何が論点か**",
    "**講師が述べた根拠・理由**",
    "**実務でどう効くか**",
    "**講師が示した具体例**",
    "**言い切られていないこと**",
]


def test_章の6項目が指示に入っている():
    for h in REQUIRED_HEADINGS:
        assert h in TPL, "★章の項目が指示から抜けています: %s" % h


REQUIRED_RULES = [
    ("音声認識の出力", "★誤変換の前提が書かれていない"),
    ("配付資料と突き合わせ", "★資料との突き合わせを指示していない"),
    ("用語の訂正", "★直したものを記録させていない"),
    ("聞き取り不確か", "★分からないものに印を付けさせていない"),
    ("自然な言葉に置き換えない", "★勝手な置き換えを禁じていない"),
    ("本文に書かない", "★日付・数値を本文から外す指示がない"),
    ("一つの章に複数の主題を混ぜない", "★主題を混ぜない指示がない"),
    ("要約しない", "★要約させない指示がない"),
    ("言及なし", "★語られていないときの書き方を指示していない"),
    ("他の章を参照しない", "★章を独立させる指示がない"),
    ("以下略", "★途中で打ち切らせない指示がない"),
]


def test_守らせることが指示に入っている():
    for word, why in REQUIRED_RULES:
        assert word in TPL, "%s（'%s' が無い）" % (why, word)


def test_章数の目安を書いていない():
    """★2026-08-20 に撤回した。数を目安に合わせると主題が混ざる。"""
    for bad in ("8〜15", "8～15", "章は5", "10個程度", "章の数は"):
        assert bad not in TPL, "★章数の目安が復活しています: %s" % bad
    assert "章の数を気にしない" in TPL, "★数に縛られない旨が書かれていない"


def test_一覧の3つを区画ごとに出させている():
    for m in (RB.MARK_TERMS, RB.MARK_NUMS, RB.MARK_LAWS):
        assert m in TPL, "★%s を出させていない" % m
    assert "明確 or 推定 or 不確か" in TPL, "★確からしさの3段階を指示していない"


def test_知識で補わせない():
    assert "知識で補いません" in SYS or "知識で補わない" in SYS
    assert "不明" in SYS, "★不明と書かせる指示がない"


# ── ② ★指示を外すと落ちること（裏取り）──────────────────────

def test_指示を1つ外すと検査が落ちる():
    """★いま通っているのは指示が実際に入っているから、の裏取り。"""
    broken = TPL.replace("一つの章に複数の主題を混ぜない", "")
    missing = [w for w, _ in REQUIRED_RULES if w not in broken]
    assert missing == ["一つの章に複数の主題を混ぜない"], (
        "★変異が効いていない（当て方の誤り）: %s" % missing)


def test_章の項目を外すと検査が落ちる():
    broken = TPL.replace("**言い切られていないこと**", "")
    assert not all(h in broken for h in REQUIRED_HEADINGS), (
        "★項目を外しても検査が通ってしまう")


# ── ③ ★区画の出力を機械的に読み取れること ──────────────────

SAMPLE = """### 【補助制度への一元化】

**何が変わるのか／何が論点か**
三類型が補助に一元化される。

**講師が述べた根拠・理由**
条文上の現れは開始の審判の条文にある。

**実務でどう効くか**
言及なし

**講師が示した具体例**
なし

**言い切られていないこと**
「細かいところかもしれません」と留保している。

<<<用語の訂正>>>
公権 → 後見
自理弁識 → 事理弁識

<<<日付・数値>>>
施行 | 令和10年夏〜秋 | 推定

<<<条文・法令>>>
民法 | 7条 | 補助開始の審判
"""


def test_章と一覧に分けて読み取れる():
    p = RB.parse_chunk_output(SAMPLE)
    assert p["chapters"].startswith("### 【補助制度への一元化】")
    assert RB.MARK_TERMS not in p["chapters"], "★一覧が本文に混ざっている"
    assert p["terms"] == ["公権 → 後見", "自理弁識 → 事理弁識"]
    assert p["numbers"] == ["施行 | 令和10年夏〜秋 | 推定"]
    assert p["laws"] == ["民法 | 7条 | 補助開始の審判"]


def test_なしと書かれていたら空になる():
    p = RB.parse_chunk_output("### 【章】\n本文\n\n<<<用語の訂正>>>\nなし\n")
    assert p["terms"] == []


def test_区画をまたいで一覧を機械的に統合する():
    """★統合に LLM を通さない（通すと落ちる）。"""
    a = RB.parse_chunk_output(SAMPLE)
    b = RB.parse_chunk_output(SAMPLE.replace("公権 → 後見", "青年貢献 → 成年後見"))
    rows = RB.merge_rows([a, b], "terms")
    assert "公権 → 後見" in rows and "青年貢献 → 成年後見" in rows
    assert rows.count("自理弁識 → 事理弁識") == 1, "★重複が落ちていない"


# ── ④ ★破綻点の検知（章の形から外れた区画）────────────────

class _R:
    def __init__(self, index, ok=True):
        self.index, self.ok = index, ok


def test_見出しの無い区画が検知される():
    parsed = [RB.parse_chunk_output(SAMPLE),
              {"chapters": "見出しの無いただの文章です。", "terms": [],
               "numbers": [], "laws": []}]
    bad = RB.chapters_missing_heading(parsed, [_R(0), _R(1)])
    assert bad == [1], "★章の形から外れた区画を拾えていない"


def test_見出しが無いことが本文に出る():
    cov = {"total_chars": 100, "read_chars": 100, "dropped_chars": 0,
           "ratio": 1.0, "chunks": 2, "chunks_ok": 2, "missing": [],
           "truncated": [], "complete": True}
    md = RB.assemble_full("題名", "全体像", ["### 【章】\n本文"], [], [], [],
                          cov, [], "", "", no_heading=[1])
    assert "章の形になっていない出力が混じっています" in md
    assert "区画1" in md


def test_章が他の章を参照していたら拾える():
    hits = RB.cross_reference_hits(["### 【章】\n前述のとおり、",
                                    "### 【章2】\n独立した文章。"])
    assert hits and hits[0][0] == 0 and hits[0][1] == "前述の"
    assert len(hits) == 1, "★独立している章まで拾っている"


# ── ⑤ ★まとめの節が揃っていること ────────────────────────

def test_決めた節が全部出る():
    cov = {"total_chars": 100, "read_chars": 100, "dropped_chars": 0,
           "ratio": 1.0, "chunks": 1, "chunks_ok": 1, "missing": [],
           "truncated": [], "complete": True}
    md = RB.assemble_full(
        "題名：テスト", "全体像です。", ["### 【章】\n本文"],
        ["公権 → 後見"], ["施行 | 令和10年 | 推定"], ["民法 | 7条 | 文脈"],
        cov, [{"from": 0, "to": 20, "sec": 20,
               "from_hhmmss": "00:00:00", "to_hhmmss": "00:00:20"}],
        "次に確かめること本文", "")
    for sec in ("## 1. この記録について", "## 2. 全体像", "## 3. 本編",
                "## 日付・数値の一覧", "## 条文・法令への言及",
                "## 用語の訂正", "## 聞き取れなかった箇所",
                "## 読んだ量と捨てた量",
                "## 事務所として次に確かめること"):
        assert sec in md, "★節が抜けています: %s" % sec
    assert "条文そのものではありません" in md, "★記録であることの明記が無い"
    assert md.count("★要突合") >= 2, "★突合の印が付いていない"


def test_該当が無い節はなしと書く():
    """★空欄にしない。"""
    assert "なし" in RB.terms_section([])
    assert "なし" in RB.numbers_section([])
    assert "なし" in RB.laws_section([])
    assert "なし" in RB.gaps_section([])


def test_まとめでは本編を再要約しない():
    """★全体像・次に確かめること には、本編を渡していないこと。"""
    for fn in ("generate_overview", "generate_next_steps", "generate_about"):
        node = next(n for n in ast.walk(TREE)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        args = [a.arg for a in node.args.args]
        assert "transcript" not in args, "★%s に文字起こしを渡している" % fn
        assert "chapters" not in args, "★%s に本編を渡している" % fn


# ── ⑥ ★実行して分かった、混ざり物を落とすこと（2026-08-20 実測）────

def test_指示文の見出しを一覧に混ぜない():
    """★実際に1回通したところ、指示文の見出しがそのまま1行目に入っていた。

    実測: 日付・数値の一覧の先頭に
      「事項 | 値 | 明確 or 推定 or 不確か」が1行として入っていた。
    """
    p = RB.parse_chunk_output(
        "### 【章】\n本文\n\n<<<日付・数値>>>\n"
        "事項 | 値 | 明確 or 推定 or 不確か\n施行 | 令和9年4月 | 明確\n")
    assert p["numbers"] == ["施行 | 令和9年4月 | 明確"], p["numbers"]


def test_直していない行を訂正に数えない():
    """★実測: 「特定補助 → 特定補助」「審判 → 審判」が混ざっていた。

    直していないものを訂正として数えると、件数が嘘になる。
    """
    p = RB.parse_chunk_output(
        "### 【章】\n本文\n\n<<<用語の訂正>>>\n"
        "直す前 → 直した後\n公権 → 後見\n特定補助 → 特定補助\n 審判 → 審判 \n")
    assert p["terms"] == ["公権 → 後見"], p["terms"]


def test_混ざり物を落とす仕掛けを外すと落ちる():
    """★いま落とせているのは仕掛けのおかげ、の裏取り。"""
    assert RB._is_header_row("事項 | 値 | 明確 or 推定 or 不確か")
    assert RB._is_noop_correction("特定補助 → 特定補助")
    assert not RB._is_noop_correction("公権 → 後見")
    assert not RB._is_header_row("施行 | 令和9年4月 | 明確")


# ── ⑦ ★公表状態（確信度とは別物）2026-08-21 ────────────────

def test_公表状態を確信度と別に拾わせている():
    """★実測 2026-08-21: 指示に公表状態の項目が無く、読んだのに落ちていた。

    該当箇所（オフレコ・フライング・まだどこにも）は区画1にあり、
    その区画は読まれていた（13/13・100%）。
    """
    assert RB.MARK_PUB in TPL, "★公表状態の枠を出させていない"
    assert "確信度" in TPL, "★確信度と別物であることを書いていない"
    i = TPL.index("**言い切られていないこと**")
    blk = TPL[i:i + 260]
    assert "別物" in blk, "★確信度の節に、別物である旨が書かれていない"


def test_公表状態の例が指示に入っている():
    for w in ("オフレコ", "ここだけの話", "まだ公表していない", "私見",
              "政府見解ではありません"):
        assert w in TPL, "★例が抜けています: %s" % w
    assert "別の言い回しも拾って" in TPL, (
        "★例だけを拾う形になっています（取りこぼします）")


def test_拾ったものを本文に混ぜさせない():
    assert "本文に溶かし込まない" in TPL, "★本文へ混ぜるなと言っていない"
    assert "「入れてはいけない」という意味ではありません" in TPL, (
        "★勝手に捨てられる恐れがあります")
    assert "判断の材料として残して" in TPL, "★残せと言っていない"


def test_公表状態を本文と別の節に出す():
    cov = {"total_chars": 10, "read_chars": 10, "dropped_chars": 0,
           "ratio": 1.0, "chunks": 1, "chunks_ok": 1, "missing": [],
           "truncated": [], "complete": True}
    md = RB.assemble_full("題名", "全体像", ["### 【章】本文"], [], [], [],
                          cov, [], "", "", None, None,
                          ["主題 | 「オフレコ」 | 00:14:57"])
    assert "## ★公的な裏付けが無い箇所" in md
    assert "00:14:57" in md
    body = md.split("## 3. 本編")[1].split("## ")[0]
    assert "オフレコ" not in body, "★本文に混ざっています"


def test_0件を無いと言い切らない():
    md = RB.publicity_section([])
    assert "拾えたのは0件" in md
    assert "「無い」という意味ではありません" in md


def test_区画の出力から公表状態を読み取れる():
    p = RB.parse_chunk_output(
        "### 【章】本文\n\n<<<公表状態>>>\n"
        "経過規定 | 「実は半分ぐらいオフレコで」 | 00:14:57\n")
    assert p["publicity"] == ["経過規定 | 「実は半分ぐらいオフレコで」 | 00:14:57"]


def test_公表状態の枠を外すと検査が落ちる():
    broken = TPL.replace(RB.MARK_PUB, "")
    assert RB.MARK_PUB not in broken, "★変異が効いていない"


# ── ⑧ ★一覧に本文が流れ込まないこと（2026-08-21 実測）────────

SPILL = """### 【章A】
本文A

<<<公表状態>>>
主題 | 発言 | 00:14:57

### 【章B】
本文B

**何が変わるのか／何が論点か**
本文B の続き
"""


def test_印の後ろに来た章は本文へ返す():
    """★実測 2026-08-21: 最後の区画が印の後ろに章を書き、
    その5章ぶん62行が「公的な裏付けが無い箇所」の表に流れ込んだ。
    件数が 4件 → ★66件 と表示され、★本編からは章が消えていた。
    """
    p = RB.parse_chunk_output(SPILL)
    assert p["publicity"] == ["主題 | 発言 | 00:14:57"], p["publicity"]
    assert "### 【章B】" in p["chapters"], "★章が本文に戻っていない"
    assert "本文B の続き" in p["chapters"], "★章の中身が落ちている"


def test_流れ込んだ行を捨てない():
    """★勝手に消さない。本文へ返す。"""
    p = RB.parse_chunk_output(SPILL)
    assert p.get("spilled"), "★戻した行数を記録していない"
    assert p["spilled"] >= 4


def test_章の項目名でも本文と分かる():
    """★見出しが無くても、章の項目名が出たら本文。"""
    p = RB.parse_chunk_output(
        "### 【章】\n本文\n\n<<<用語の訂正>>>\n公権 → 後見\n"
        "**講師が述べた根拠・理由**\n理由の本文\n")
    assert p["terms"] == ["公権 → 後見"]
    assert "理由の本文" in p["chapters"]


def test_仕掛けを外すと流れ込む():
    """★いま防げているのは仕掛けのおかげ、の裏取り。"""
    assert RB._looks_like_body("**何が変わるのか／何が論点か**")
    assert not RB._looks_like_body("公権 → 後見")
    assert RB._HEADING.match("### 【章B】")


def test_正しい件数になること():
    """★件数が嘘にならないこと。"""
    p = RB.parse_chunk_output(SPILL)
    md = RB.publicity_section(p["publicity"])
    assert "★1件" in md, md.split("\n")[2]
