# -*- coding: utf-8 -*-
r"""★研修DBの題名を「この記録について」から取る（2026-08-21 追加）。

★これは受け側検証（正本: 共有CLAUDE.md「★検査・テスト・監視を作るとき / 直すとき」）。
突き合わせ元: レポート本文の「## 1. この記録について」に書かれた 題名 / 日時 / 講師 / 主催
突き合わせ先: ★save_to_notion_kenshu が Notion に渡す properties の中身（実物の構文）
  ＝ 送り手（取り出しの関数）が使っていない経路で、渡る先を見る

■ なぜ要るか（★実測 2026-08-21）
  題名はレポート1行目の見出し `# 講演の記録` から取っていた。
  ★見出しはどのレポートでも同じ文字列なので、研修DBの題名が全部
  「講演の記録」になり、講演を見分けられなくなった。
  実際に1行（page 3c367ba3-0447-8139-9f0f-f98460718aca）がこの名前で入った。

■ ★破綻点（先に挙げてから作った）
  「この記録について」の書き方が変われば取れなくなる。
  そのとき★黙って見出しに戻すと、また同じことが起きる。
  検知: ①見出しを題名にする形に戻すと落ちる（変異）
        ②取れないときに missing に理由が入る
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

FULL = """# 講演の記録

## 1. この記録について

- 題名: 成年後見制度改正の最新動向（民法改正を視野に）
- 日時: 令和 8 年 4 月 22 日（水）17 時 30 分～19 時 30 分
- 講師: 弁護士 根本 雄司 先生（弁護士法人 港大さん橋法律事務所）
- 主催: 公益社団法人コスモス成年後見サポートセンター神奈川県支部

## 2. 全体像
"""

NO_TITLE = FULL.replace(
    "- 題名: 成年後見制度改正の最新動向（民法改正を視野に）\n", "")
UNKNOWN = FULL.replace("成年後見制度改正の最新動向（民法改正を視野に）", "不明")
BARE = "# 講演の記録\n\n## 2. 全体像\n本文だけ\n"


# ── ① ★題名は「この記録について」から取る ──────────────────

def test_題名を取り出す():
    assert RB.parse_about(FULL)["題名"] == "成年後見制度改正の最新動向（民法改正を視野に）"


def test_研修DBに渡る題名が見出しでない():
    t, miss = RB.title_from_report(FULL)
    assert t == "成年後見制度改正の最新動向（民法改正を視野に）"
    assert t != "講演の記録", "★見出しを題名にしています"
    assert miss == []


def test_日時講師主催も取れる():
    a = RB.parse_about(FULL)
    assert a["講師"].startswith("弁護士 根本")
    assert a["主催"].startswith("公益社団法人コスモス")
    assert "令和 8 年 4 月 22 日" in a["日時"]


def test_不明と書かれていたら値にしない():
    """★「不明」という文字を題名にしない。"""
    a = RB.parse_about(UNKNOWN)
    assert "題名" not in a, a
    t, _ = RB.title_from_report(UNKNOWN)
    assert t != "不明" and t != "講演の記録"


# ── ② ★取れないときに見出しへ戻さない ────────────────────

def test_題名が無くても見出しに戻らない():
    t, miss = RB.title_from_report(NO_TITLE)
    assert "講演の記録" not in t, "★見出しに戻っています: %s" % t
    assert any("題名が書かれていません" in m for m in miss), miss


def test_題名が無いときは講師と日付から組み立てる():
    """★何を使ったかが題名を見て分かること。"""
    t, _ = RB.title_from_report(NO_TITLE)
    assert t.startswith("（題名なし）"), t
    assert "2026-04-22" in t, t
    assert "根本" in t, t


def test_何も取れなければ空を返す():
    """★勝手に既定の名前を付けない。"""
    t, miss = RB.title_from_report(BARE)
    assert t == ""
    assert any("題名を作れません" in m for m in miss), miss


def test_取れなかった理由が足りないものに入る(tmp_path):
    p = tmp_path / "r.md"
    p.write_text(BARE, encoding="utf-8")
    b = RB.load_report_bundle(str(p))
    assert b["title"] == ""
    assert any("題名" in m for m in b["missing"]), b["missing"]
    assert "★足りないもの" in RB.bundle_readiness(b)


# ── ③ ★変異：見出しに戻すと落ちること ────────────────────

def test_見出しを題名にする形に戻すと落ちる():
    """★仕掛けの裏取り。これが通ると、上の検査は何も守っていない。"""
    def broken(report):
        for line in report.splitlines():
            if line.startswith("# ") and not line.startswith("## "):
                return line[2:].strip()
        return ""
    assert broken(FULL) == "講演の記録"
    assert RB.title_from_report(FULL)[0] != broken(FULL), "★変異が効いていない"
    # ★見出しだけのレポートでも、見出しは題名にならない
    assert RB.title_from_report(BARE)[0] != broken(BARE)


def test_実装が見出しを拾っていない():
    """★extract_title_from_report が自前で `# ` を探していないこと。"""
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef)
              and n.name == "extract_title_from_report")
    body = ast.unparse(fn)
    assert "title_from_report" in body, "★1か所に寄せていません"
    assert 'startswith("# ")' not in body, "★見出しを拾う形が残っています"
    assert "音声メモレポート" not in body, "★既定の名前で埋めています"


# ── ④ ★日時は「実施日」に入り、作った日で埋めないこと ──────────

def test_和暦を西暦に直す():
    assert RB.parse_event_date("令和 8 年 4 月 22 日（水）17 時") == "2026-04-22"
    assert RB.parse_event_date("平成三十一年一月五日") == "2019-01-05"
    assert RB.parse_event_date("令和元年5月1日") == "2019-05-01"
    assert RB.parse_event_date("2026/4/22") == "2026-04-22"


def test_読めない日付は推測しない():
    assert RB.parse_event_date("先日") is None
    assert RB.parse_event_date("") is None


def _save_fn():
    return next(n for n in ast.walk(TREE)
                if isinstance(n, ast.FunctionDef)
                and n.name == "save_to_notion_kenshu")


def test_実施日を今日で埋めない():
    """★実施日は講演が行われた日。作った日ではない。
    ★読めないときは空のまま（未確認と分かる）。
    """
    body = ast.unparse(_save_fn())
    assert "'実施日': {'date': {'start': date_iso}}" not in body, (
        "★実施日を今日の日付で埋めています")
    assert "parse_event_date" in body, "★講演の日を見ていません"
    assert "if _event:" in body, "★読めないときも入れてしまいます"


def test_作成日は今日のままであること():
    assert "'作成日': {'date': {'start': date_iso}}" in ast.unparse(_save_fn())


# ── ⑤ ★新しい欄を作っていないこと ──────────────────────

#: 2026-08-21 に Notion から実測した研修DBの欄（12個）。
KENSHU_FIELDS = {"投入可", "実施日", "投入日", "作成日", "タグ", "ファイル名",
                 "概要", "種別", "検証状況", "ジャンル", "タイトル", "GDriveリンク"}


def test_研修DBに無い欄へ書いていない():
    fn = _save_fn()
    used = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                and n.value.id == "properties" \
                and isinstance(n.slice, ast.Constant):
            used.add(n.slice.value)
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                        and k.value in KENSHU_FIELDS:
                    used.add(k.value)
    assert used, "★欄を1つも見ていません"
    assert used <= KENSHU_FIELDS, "★研修DBに無い欄を作ろうとしています: %s" % (
        used - KENSHU_FIELDS)


def test_講師と主催は概要の冒頭に入る():
    """★対応する欄が無いので、既存の概要に前置きする。★概要は消さない。"""
    body = ast.unparse(_save_fn())
    assert "'講師', '主催'" in body or '"講師", "主催"' in body, (
        "★講師・主催を渡していません")
    assert "_head" in body and "summary or ''" in body, (
        "★元の概要を捨てています")


def test_両方の入口がaboutを渡す():
    calls = [c for c in ast.walk(TREE)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id == "save_to_notion_kenshu"]
    assert len(calls) == 2
    for c in calls:
        assert "about" in {k.arg for k in c.keywords}, "★片方だけ直っています"
