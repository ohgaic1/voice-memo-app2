import streamlit as st
import tempfile
import os
import re
import json
from pathlib import Path
from datetime import datetime
import subprocess
from openai import OpenAI
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────
# ページ設定
# ─────────────────────────────────────────
st.set_page_config(
    page_title="音声メモアプリ Pro",
    page_icon="🎙️",
    layout="wide"
)

if "api_key" not in st.session_state:
    # 優先順位: 1. 環境変数 / 2. st.secrets / 3. 空文字（サイドバー入力へ）
    _key = os.environ.get("OPENAI_API_KEY", "")
    if not _key:
        try:
            _key = st.secrets.get("OPENAI_API_KEY", "")
        except Exception:
            _key = ""
    st.session_state.api_key = _key
if "results" not in st.session_state:
    st.session_state.results = []

# Notion設定（環境変数から読み込み）
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "")
NOTION_DB_KENSHU   = os.environ.get("NOTION_DB_ID_KENSHU", "475162c3cf1f4993a9b231e202ec40fb")


# ═══════════════════════════════════════════
# トークン制限対策：長いテキストを事前圧縮
# ═══════════════════════════════════════════
MAX_TRANSCRIPT_CHARS = 12000   # GPTに送る文字起こしの上限
MAX_MATERIAL_CHARS   = 3000    # 資料テキストの上限
MAX_REPORT_CHARS     = 4000    # サマリー生成時のレポートの上限


def compress_transcript(text: str, api_key: str) -> str:
    """
    文字起こしが長すぎる場合、GPTで事前に要点を圧縮する。
    圧縮後は MAX_TRANSCRIPT_CHARS 以内に収める。
    """
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text   # 短ければそのまま返す

    client = OpenAI(api_key=api_key)
    # 長い場合は先頭・中盤・末尾から均等にサンプリング
    third = len(text) // 3
    sampled = (
        text[:4000] + "\n...(中略)...\n"
        + text[third: third + 4000] + "\n...(中略)...\n"
        + text[-4000:]
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",   # 圧縮はminiで十分
            messages=[
                {"role": "system", "content": "あなたは会議の内容を忠実に要約するアシスタントです。"},
                {"role": "user", "content":
                    f"以下の音声文字起こしを、重要な情報を落とさず8000文字以内に圧縮してください。"
                    f"発言の流れ・決定事項・数値・固有名詞は必ず残してください。\n\n{sampled}"}
            ],
            temperature=0.2,
            max_tokens=4000
        )
        compressed = resp.choices[0].message.content
        st.info(f"📝 文字起こしを圧縮しました（{len(text):,}文字 → {len(compressed):,}文字）")
        return compressed
    except Exception:
        # 圧縮失敗時はシンプルにカット
        st.warning("⚠️ 圧縮に失敗したため先頭部分のみ使用します。")
        return text[:MAX_TRANSCRIPT_CHARS]


# ═══════════════════════════════════════════
# 音声処理
# ═══════════════════════════════════════════
def compress_audio(input_path, output_path):
    try:
        subprocess.run(
            ["ffmpeg", "-i", input_path, "-vn", "-ac", "1",
             "-ar", "16000", "-b:a", "32k", "-y", output_path],
            check=True, capture_output=True
        )
        return True
    except Exception as e:
        st.error(f"圧縮エラー: {e}")
        return False


def split_audio(input_path, chunk_sec=600):
    """音声を10分チャンクに分割"""
    chunks = []
    try:
        # 時間を取得
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True, check=True
        )
        duration = float(probe.stdout.strip())
        num_chunks = int(duration / chunk_sec) + 1
        
        # 拡張子に関わらず.mp3で統一
        base = os.path.splitext(input_path)[0]
        
        for i in range(num_chunks):
            output_chunk = f"{base}_chunk{i}.mp3"
            subprocess.run(
                ["ffmpeg", "-i", input_path,
                 "-ss", str(i * chunk_sec), "-t", str(chunk_sec),
                 "-c", "copy", "-y", output_chunk],
                check=True, capture_output=True
            )
            if os.path.exists(output_chunk) and os.path.getsize(output_chunk) > 1000:
                chunks.append(output_chunk)
        
        return chunks
    except Exception as e:
        st.error(f"分割エラー: {e}")
        return []


def transcribe_audio(file_path, api_key):
    """音声ファイルを文字起こし（大容量対応）"""
    client = OpenAI(api_key=api_key)
    max_size = 24 * 1024 * 1024
    
    try:
        # ファイルサイズチェック
        size = os.path.getsize(file_path)
        work_path = file_path
        
        # 圧縮が必要な場合
        if size > max_size:
            st.info("  🔧 圧縮中...")
            # 拡張子を.mp3に統一
            base = os.path.splitext(file_path)[0]
            comp = f"{base}_comp.mp3"
            if not compress_audio(file_path, comp):
                return None
            work_path = comp
            size = os.path.getsize(work_path)
        
        # まだ大きければ分割
        if size > max_size:
            st.info("  ✂️ 分割中...")
            chunks = split_audio(work_path)
            if not chunks:
                return None
            
            texts = []
            pb = st.progress(0)
            for i, chunk in enumerate(chunks):
                with open(chunk, "rb") as f:
                    resp = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                        language="ja"
                    )
                    texts.append(resp.text)
                pb.progress((i + 1) / len(chunks))
                os.remove(chunk)
            
            # 圧縮ファイルを削除
            if work_path != file_path and os.path.exists(work_path):
                os.remove(work_path)
            
            return " ".join(texts).strip()
        
        # 通常サイズ → そのまま文字起こし
        with open(work_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ja"
            )
        
        # 圧縮ファイルを削除
        if work_path != file_path and os.path.exists(work_path):
            os.remove(work_path)
        
        return resp.text
        
    except Exception as e:
        st.error(f"文字起こしエラー: {e}")
        return None


# ═══════════════════════════════════════════
# 資料テキスト抽出
# ═══════════════════════════════════════════
def extract_pdf_text(file_path):
    try:
        import pdfplumber
        texts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    texts.append(t)
        return "\n".join(texts)
    except Exception:
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        except Exception as e:
            return f"[PDF読み取りエラー: {e}]"


def extract_pptx_text(file_path):
    try:
        from pptx import Presentation
        prs = Presentation(file_path)
        texts = []
        for i, slide in enumerate(prs.slides, 1):
            parts = [s.text.strip() for s in slide.shapes
                     if hasattr(s, "text") and s.text.strip()]
            if parts:
                texts.append(f"【スライド{i}】\n" + "\n".join(parts))
        return "\n\n".join(texts)
    except Exception as e:
        return f"[PPTX読み取りエラー: {e}]"


def extract_docx_text(file_path):
    try:
        from docx import Document
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        return f"[DOCX読み取りエラー: {e}]"


def extract_material_text(uploaded_file):
    suffix = Path(uploaded_file.name).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        f.write(uploaded_file.read())
        tmp = f.name
    try:
        if suffix == ".pdf":
            return extract_pdf_text(tmp)
        elif suffix in [".pptx", ".ppt"]:
            return extract_pptx_text(tmp)
        elif suffix in [".docx", ".doc"]:
            return extract_docx_text(tmp)
        return f"[未対応形式: {suffix}]"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ═══════════════════════════════════════════
# YouTube字幕取得
# ═══════════════════════════════════════════
def extract_youtube_id(url: str) -> str | None:
    patterns = [
        r'(?:v=|/v/|youtu\.be/|/embed/)([^&\n?#]+)',
        r'(?:shorts/)([^&\n?#]+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def _fetch_entries_text(entries) -> str:
    """FetchedTranscript エントリ（v1.x オブジェクト or v0.x 辞書）からテキストを結合"""
    parts = []
    for e in entries:
        if hasattr(e, "text"):
            parts.append(e.text)
        elif isinstance(e, dict):
            parts.append(e.get("text", ""))
    return " ".join(parts)


def _youtube_whisper_fallback(url: str, video_id: str) -> tuple:
    """字幕不可の場合に yt-dlp で音声ダウンロード → Whisper 文字起こしを試みる"""
    try:
        import yt_dlp  # optional dependency
    except ImportError:
        return (
            None,
            f"字幕が見つかりません（ID: {video_id}）。\n"
            "音声からの文字起こしを行うには yt-dlp が必要です: pip install yt-dlp",
        )

    st.info("💬 字幕が見つかりません。yt-dlp で音声をダウンロードして Whisper で文字起こしを試みます...")
    try:
        import tempfile, glob
        with tempfile.TemporaryDirectory() as tmpdir:
            out_tmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            ydl_opts = {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": out_tmpl,
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            files = glob.glob(os.path.join(tmpdir, "*"))
            if not files:
                return None, "yt-dlp でのダウンロードに失敗しました"
            audio_path = files[0]
            api_key = st.session_state.get("api_key", "")
            if not api_key:
                return None, "音声ダウンロード成功しましたが OpenAI API キーがないため Whisper 文字起こしができません"
            transcript = transcribe_audio(audio_path, api_key)
            if transcript:
                st.success(f"✅ Whisper フォールバック成功（{len(transcript):,}文字）")
                return transcript, video_id
            return None, "Whisper 文字起こしに失敗しました"
    except Exception as e:
        return None, f"音声ダウンロード / 文字起こしエラー: {str(e)[:200]}"


def get_youtube_transcript(url: str) -> tuple:
    """YouTube URL から字幕テキストを取得。字幕不可なら Whisper フォールバック。
    Returns: (text_or_None, video_id_or_error_message)
    """
    # ── パッケージ確認 ──
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
            InvalidVideoId,
        )
    except ImportError as ie:
        return None, f"youtube-transcript-api がインストールされていません: {ie}"

    # v1.x の新エラー（v0.x では存在しない場合がある）
    try:
        from youtube_transcript_api._errors import RequestBlocked, IpBlocked, AgeRestricted
        _HAS_V1_ERRORS = True
    except ImportError:
        RequestBlocked = IpBlocked = AgeRestricted = Exception  # 型チェック用ダミー
        _HAS_V1_ERRORS = False

    # ── URL からビデオ ID 抽出 ──
    video_id = extract_youtube_id(url)
    if not video_id:
        return None, (
            "YouTube URL からビデオ ID を抽出できませんでした。\n"
            "対応形式: https://www.youtube.com/watch?v=XXXXX "
            "/ https://youtu.be/XXXXX / Shorts URL"
        )

    # ── 字幕取得（v1.x インスタンスメソッド） ──
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)

        # 優先度: ①手動日本語 → ②自動生成日本語 → ③英語 → ④何でも最初の1件
        transcript = None
        for finder, langs in [
            ("find_manually_created_transcript", ["ja", "ja-JP"]),
            ("find_generated_transcript",        ["ja", "ja-JP"]),
            ("find_transcript",                  ["en", "en-US", "en-GB"]),
        ]:
            try:
                transcript = getattr(transcript_list, finder)(langs)
                break
            except Exception:
                continue

        if transcript is None:
            for t in transcript_list:
                transcript = t
                break

        if transcript is None:
            return _youtube_whisper_fallback(url, video_id)

        entries = transcript.fetch()
        text = _fetch_entries_text(entries)
        lang = getattr(transcript, "language", "不明")
        is_gen = getattr(transcript, "is_generated", False)
        st.caption(f"📝 字幕言語: {lang}（{'自動生成' if is_gen else '手動作成'}）")
        return text, video_id

    except TranscriptsDisabled:
        st.warning("⚠️ この動画では字幕が無効です。Whisper フォールバックを試みます...")
        return _youtube_whisper_fallback(url, video_id)

    except NoTranscriptFound:
        st.warning("⚠️ 字幕が見つかりません。Whisper フォールバックを試みます...")
        return _youtube_whisper_fallback(url, video_id)

    except VideoUnavailable:
        return None, f"動画が存在しないか非公開です（ID: {video_id}）"

    except InvalidVideoId:
        return None, f"無効なビデオ ID です: {video_id}"

    except Exception as e:
        err_str = str(e)
        # v1.x 特有のエラーを文字列でもチェック（_HAS_V1_ERRORS が False の場合の保険）
        if _HAS_V1_ERRORS and isinstance(e, AgeRestricted):
            return None, "年齢制限のある動画は字幕取得できません（認証未対応）"
        if _HAS_V1_ERRORS and isinstance(e, (RequestBlocked, IpBlocked)):
            return None, (
                "YouTube にアクセスがブロックされました（IP 制限）。\n"
                "しばらく待つか、別のネットワークから試してください。"
            )
        if "429" in err_str or "too many requests" in err_str.lower():
            return None, "リクエスト過多です（429）。しばらく待ってから再試行してください。"
        if "age" in err_str.lower() and "restrict" in err_str.lower():
            return None, "年齢制限のある動画は字幕取得できません"
        if "unavailable" in err_str.lower() or "private" in err_str.lower():
            return None, f"動画が利用不可または非公開です: {err_str[:100]}"
        return None, f"字幕取得エラー: {err_str[:200]}"


# ═══════════════════════════════════════════
# レポートからタイトル・タグを抽出
# ═══════════════════════════════════════════
def extract_title_from_report(report: str) -> str:
    for line in report.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return "音声メモレポート"


def extract_tags_from_report(report: str) -> list[str]:
    for line in report.splitlines():
        line = line.strip()
        if line.startswith("> タグ：") or line.startswith("> タグ:"):
            tags_str = re.sub(r'^> タグ[：:]', '', line).strip()
            return [t.strip() for t in re.split(r'[、,，　 ]+', tags_str) if t.strip()]
    return []


# ═══════════════════════════════════════════
# Markmap生成
# ═══════════════════════════════════════════
def generate_markmap(report: str, api_key: str) -> str | None:
    """PLAUDレポートからMarkmap用Markdown見出し構造を生成"""
    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたはMarkdown見出し構造のみを出力する専門家です。コードブロックや説明文は一切不要です。"},
                {"role": "user", "content": f"""以下のレポートをMarkmap形式のMarkdown見出し構造に変換してください。
ルール：
- 見出し（# ## ###）のみ使用
- 各ノードは短いキーワード（15文字以内）
- コードブロック不要、見出しのみ出力
- 深さは最大3階層

レポート（抜粋）:
{report[:5000]}"""}
            ],
            temperature=0.3,
            max_tokens=800,
        )
        return resp.choices[0].message.content
    except Exception as e:
        return None


def render_markmap_html(markmap_md: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@latest"></script>
<style>
html,body{{margin:0;padding:0;width:100%;height:600px;overflow:hidden;}}
#mm{{width:100%;height:600px;}}
</style>
</head>
<body>
<div class="markmap" id="mm">

{markmap_md}

</div>
<script>
window.addEventListener('load', function() {{
  setTimeout(function() {{
    var svgs = document.querySelectorAll('svg.markmap');
    if (svgs.length > 0) {{
      try {{
        var mmInstance = svgs[0].__markmap;
        if (mmInstance && mmInstance.fit) {{ mmInstance.fit(); }}
      }} catch(e) {{}}
    }}
  }}, 800);
}});
</script>
</body>
</html>"""


# ═══════════════════════════════════════════
# Notion ブロックヘルパー
# ═══════════════════════════════════════════
def _rich_text(content: str) -> list:
    chunks = [content[i:i+1990] for i in range(0, len(content), 1990)]
    return [{"type": "text", "text": {"content": c}} for c in chunks[:10]]

def _heading_block(level: int, text: str) -> dict:
    t = f"heading_{level}"
    return {"object": "block", "type": t, t: {"rich_text": _rich_text(text[:200])}}

def _paragraph_block(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(text[:2000])}}

def _bulleted_block(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich_text(text[:2000])}}

def _numbered_block(text: str) -> dict:
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": _rich_text(text[:2000])}}

def _quote_block(text: str) -> dict:
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": _rich_text(text[:2000])}}

def _divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}

def _code_block(content: str, language: str = "markdown") -> dict:
    return {"object": "block", "type": "code",
            "code": {"rich_text": _rich_text(content[:2000]), "language": language}}


def markdown_to_notion_blocks(md: str) -> list:
    """マークダウン文字列をNotionブロックリストに変換"""
    blocks = []
    for line in md.splitlines():
        if line.startswith("# "):
            blocks.append(_heading_block(1, line[2:].strip()))
        elif line.startswith("## "):
            blocks.append(_heading_block(2, line[3:].strip()))
        elif line.startswith("### "):
            blocks.append(_heading_block(3, line[4:].strip()))
        elif re.match(r'^\d+\. ', line):
            text = re.sub(r'^\d+\. ', '', line).strip()
            blocks.append(_numbered_block(text))
        elif line.startswith("- ") or line.startswith("* "):
            text = line[2:].strip()
            blocks.append(_bulleted_block(text))
        elif line.startswith("> "):
            text = line[2:].strip()
            blocks.append(_quote_block(text))
        elif line.strip() == "---":
            blocks.append(_divider_block())
        elif line.strip():
            text = re.sub(r'`([^`]+)`', r'\1', line)
            text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
            text = re.sub(r'\*([^*]+)\*', r'\1', text)
            blocks.append(_paragraph_block(text))
    return blocks


def _append_blocks(page_id: str, blocks: list, headers: dict) -> None:
    """90ブロックのバッチに分けてNotionページに追記"""
    for i in range(0, len(blocks), 90):
        batch = blocks[i:i+90]
        resp = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": batch},
            timeout=30,
        )
        if not resp.ok:
            raise ValueError(f"ブロック追記エラー {resp.status_code}: {resp.text[:200]}")


# ═══════════════════════════════════════════
# Notion 研修DB への保存
# ═══════════════════════════════════════════
def save_to_notion_kenshu(
    title: str,
    tags: list,
    source_type: str,
    report: str,
    summary: str,
    transcript: str = "",
    markmap_md: str = "",
    summary_data: dict = None,
    source_info: list = None,
    attachment_file_info: list = None,
) -> bool:
    if not NOTION_API_KEY:
        st.error("⚠️ NOTION_API_KEY が未設定です。環境変数を確認してください。")
        return False

    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    date_iso = datetime.now().strftime("%Y-%m-%d")

    properties: dict = {
        "タイトル": {"title": [{"text": {"content": title[:200]}}]},
        "ジャンル": {"select": {"name": "その他"}},
        "種別":   {"select": {"name": source_type}},
        "実施日": {"date": {"start": date_iso}},
        "作成日": {"date": {"start": date_iso}},
        "概要":   {"rich_text": [{"text": {"content": summary[:500]}}]},
    }
    if tags:
        properties["タグ"] = {"multi_select": [{"name": t[:100]} for t in tags[:5]]}

    # ── ページ作成（本文なし）──
    try:
        resp = requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json={"parent": {"database_id": NOTION_DB_KENSHU}, "properties": properties},
            timeout=30,
        )
        if not resp.ok:
            msg = resp.json().get("message", resp.text[:200])
            st.error(f"Notion保存エラー: {resp.status_code} - {msg}")
            return False
        page_data = resp.json()
        page_id = page_data["id"]
        page_url = page_data.get("url", "")
    except Exception as e:
        st.error(f"Notion保存エラー: {e}")
        return False

    try:
        # ① 元データ
        src_blocks = [_heading_block(2, "① 元データ")]
        if source_info:
            for s in source_info:
                src_blocks.append(_bulleted_block(s))
        else:
            src_blocks.append(_paragraph_block("（情報なし）"))
        _append_blocks(page_id, src_blocks, headers)

        # ② 添付資料
        att_blocks = [_divider_block(), _heading_block(2, "② 添付資料")]
        if attachment_file_info:
            for fi in attachment_file_info:
                size_kb = fi.get("size", 0) // 1024
                att_blocks.append(_bulleted_block(f"{fi['name']}  ({size_kb} KB)"))
        else:
            att_blocks.append(_paragraph_block("（なし）"))
        _append_blocks(page_id, att_blocks, headers)

        # ③ 文字起こし（トグルブロック）
        toggle_header = [
            _divider_block(),
            {"object": "block", "type": "toggle",
             "toggle": {"rich_text": _rich_text("③ 文字起こし（クリックで展開）")}},
        ]
        tog_resp = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": toggle_header},
            timeout=30,
        )
        if tog_resp.ok and transcript.strip():
            toggle_id = None
            for b in tog_resp.json().get("results", []):
                if b.get("type") == "toggle":
                    toggle_id = b["id"]
                    break
            if toggle_id:
                tr_chunks = [transcript[i:i+1990] for i in range(0, min(len(transcript), 60000), 1990)]
                tr_blocks = [_paragraph_block(c) for c in tr_chunks[:90]]
                _append_blocks(toggle_id, tr_blocks, headers)

        # ④ PLAUDレポート
        report_header = [_divider_block(), _heading_block(2, "④ PLAUDレポート")]
        _append_blocks(page_id, report_header, headers)
        report_blocks = markdown_to_notion_blocks(report)
        _append_blocks(page_id, report_blocks[:200], headers)

        # ⑤ マインドマップ
        if markmap_md:
            mm_blocks = [_divider_block(), _heading_block(2, "⑤ マインドマップ（Markmap）")]
            mm_chunks = [markmap_md[i:i+1990] for i in range(0, min(len(markmap_md), 10000), 1990)]
            for chunk in mm_chunks[:5]:
                mm_blocks.append(_code_block(chunk, "markdown"))
            _append_blocks(page_id, mm_blocks, headers)

        # ⑥ 構造化サマリー
        if summary_data:
            sum_blocks = [_divider_block(), _heading_block(2, "⑥ 構造化サマリー")]
            if summary_data.get("one_line"):
                sum_blocks.append(_paragraph_block(f"📌 {summary_data['one_line']}"))
            if summary_data.get("flow"):
                sum_blocks.append(_heading_block(3, "フロー"))
                for item in summary_data["flow"]:
                    sum_blocks.append(_bulleted_block(
                        f"【{item.get('time','')}】{item.get('topic','')} — {item.get('summary','')}"
                    ))
            if summary_data.get("decisions"):
                sum_blocks.append(_heading_block(3, "決定事項"))
                for d in summary_data["decisions"]:
                    sum_blocks.append(_bulleted_block(f"✅ {d.get('title','')}：{d.get('detail','')}"))
            if summary_data.get("actions"):
                sum_blocks.append(_heading_block(3, "アクションアイテム"))
                for a in summary_data["actions"]:
                    sum_blocks.append(_bulleted_block(
                        f"[{a.get('priority','')}] {a.get('what','')} — {a.get('who','未定')} / {a.get('when','期限未定')}"
                    ))
            if summary_data.get("keywords"):
                sum_blocks.append(_heading_block(3, "キーワード"))
                sum_blocks.append(_paragraph_block("  ".join(summary_data["keywords"])))
            _append_blocks(page_id, sum_blocks, headers)

        st.success(f"✅ Notionに保存しました！ [ページを開く]({page_url})")
        return True

    except Exception as e:
        st.error(f"Notion本文追記エラー: {e}")
        return False


# ═══════════════════════════════════════════
# GPT：Plaud風レポート
# ═══════════════════════════════════════════
def generate_report(combined_transcript, file_labels, material_text, api_key):
    client = OpenAI(api_key=api_key)

    # ── 入力テキストを制限内に収める ──
    safe_transcript = combined_transcript[:MAX_TRANSCRIPT_CHARS]
    if len(combined_transcript) > MAX_TRANSCRIPT_CHARS:
        st.info(f"📝 レポート生成のため文字起こしを {MAX_TRANSCRIPT_CHARS:,}文字に調整しました（元: {len(combined_transcript):,}文字）")

    safe_material = ""
    if material_text and material_text.strip():
        safe_material = f"""
---
【補足資料】
{material_text[:MAX_MATERIAL_CHARS]}
---
上記資料の数値・固有名詞・用語を積極的に活用してください。
"""

    files_note = (
        f"※ 本レポートは以下 {len(file_labels)} 件の音声ファイルを統合した内容です：\n"
        + "\n".join(f"  - {l}" for l in file_labels)
    ) if len(file_labels) > 1 else ""

    prompt = f"""以下の音声文字起こしを全て読み込み、PLAUD形式の詳細レポートを日本語で作成してください。
{files_note}
{safe_material}
【文字起こし】
{safe_transcript}

---

以下のフォーマットに厳密に従って出力してください。

# {{タイトル（内容を表す簡潔なタイトル）}}

> 日時：{{録音日時（不明な場合は「不明」）}}
> タグ：{{内容から自動生成した3〜5個のキーワードをカンマ区切りで記載}}

## テーマ
{{全体の内容を2〜3文で要約}}

## 要点
1. {{要点1}}
2. {{要点2}}
3. {{要点3}}
（内容に応じて5〜10個）

## ハイライト
- `"{{文字起こしの原文をそのまま引用1}}"`
- `"{{文字起こしの原文をそのまま引用2}}"`
- `"{{文字起こしの原文をそのまま引用3}}"`
（印象的・重要な発言を3〜6個、必ず原文をそのまま引用すること）

## 章とトピック
### {{章タイトル1（文字起こしの流れに沿ったトピック名）}}
> {{章の概要を2〜3文}}
- **要点**
  - {{要点箇条書き}}
- **説明**
  {{詳細説明}}
- **Examples**
  > {{具体例の引用または説明}}
  - {{実務上の対応や示唆}}
- **留意点**
  - {{注意事項}}
- **特別な状況**
  - {{もし〜の場合の対応（該当する場合のみ記載）}}

### {{章タイトル2}}
（トピックごとに同じ構造で繰り返す）

## 宿題と提案
- {{会話中に出てきた具体的なアクションアイテム1}}
- {{会話中に出てきた具体的なアクションアイテム2}}
（該当がない場合は「特になし」と記載）

---
【作成上の注意】
- 文字起こしにない情報は追加しないこと
- ハイライトは文字起こしの原文を一字一句そのまま引用すること
- 章とトピックは文字起こしの話題の流れに沿って分割すること
- 宿題と提案は会話中に明示的に出てきたアクションのみ記載すること
- 全て日本語で出力すること"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは音声メモからPLAUD形式の高品質な構造化レポートを作成する専門家です。指定されたフォーマットに厳密に従い、文字起こしの内容を網羅的に整理してください。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=4000
        )
        return resp.choices[0].message.content
    except Exception as e:
        error_str = str(e)
        if "rate_limit_exceeded" in error_str or "too large" in error_str.lower():
            st.warning("⚠️ テキストが長すぎるため、さらに短縮して再試行します...")
            short_transcript = combined_transcript[:6000]
            try:
                resp2 = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "あなたは音声メモからPLAUD形式の高品質な構造化レポートを作成する専門家です。指定されたフォーマットに厳密に従い、文字起こしの内容を網羅的に整理してください。"},
                        {"role": "user", "content": prompt.replace(safe_transcript, short_transcript)}
                    ],
                    temperature=0.3,
                    max_tokens=4000
                )
                return resp2.choices[0].message.content
            except Exception as e2:
                st.error(f"レポート生成エラー（再試行後）: {e2}")
                return None
        st.error(f"レポート生成エラー: {e}")
        return None


# ═══════════════════════════════════════════
# GPT：構造化サマリー（JSON）
# ═══════════════════════════════════════════
def generate_summary_json(combined_transcript, report, material_text, api_key):
    client = OpenAI(api_key=api_key)

    # ── 入力を制限 ──
    safe_transcript = combined_transcript[:6000]
    safe_report     = report[:MAX_REPORT_CHARS]
    mat_note = "補足資料の情報も反映してください。" if material_text else ""

    prompt = f"""以下の音声文字起こしとレポートから、構造化サマリーをJSON形式で作成してください。{mat_note}

【文字起こし（抜粋）】
{safe_transcript}

【レポート（抜粋）】
{safe_report}

以下のJSON構造で出力してください（日本語で）。
コードブロック（```）は使わず、JSONのみを出力してください。

{{
  "title": "会議・メモのタイトル（15〜30文字）",
  "date": "推定日付または「不明」",
  "type": "会議 / 1on1 / ブレスト / 講義 / その他",
  "duration": "推定XX分",
  "urgency": "高 / 中 / 低",
  "one_line": "この会議・メモを一文で表すと（30〜50文字）",
  "participants": ["参加者1", "参加者2"],
  "flow": [
    {{"time": "序盤", "topic": "トピック名", "summary": "内容の要約（30〜60文字）"}},
    {{"time": "中盤", "topic": "トピック名", "summary": "内容の要約（30〜60文字）"}},
    {{"time": "終盤", "topic": "トピック名", "summary": "内容の要約（30〜60文字）"}}
  ],
  "decisions": [
    {{"title": "決定事項名", "detail": "詳細説明"}}
  ],
  "actions": [
    {{"priority": "高/中/低", "who": "担当者", "what": "タスク内容", "when": "期限"}}
  ],
  "concerns": [
    {{"title": "懸念・リスク名", "detail": "詳細説明"}}
  ],
  "next_topics": ["次回以降の検討事項1", "次回以降の検討事項2"],
  "key_numbers": [
    {{"label": "指標名・数値名", "value": "具体的な数値・データ"}}
  ],
  "keywords": ["重要キーワード1", "重要キーワード2", "重要キーワード3", "重要キーワード4", "重要キーワード5"]
}}

注意：
- decisionsは実際に決定したことのみ。なければ空配列[]
- actionsは具体的なタスク。なければ空配列[]
- concernsはリスク・懸念・未解決事項。なければ空配列[]
- key_numbersは具体的な数値が言及された場合のみ。なければ空配列[]
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは会議の内容を正確に構造化するアナリストです。指示通りのJSONのみを出力します。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=2000,
            response_format={"type": "json_object"}
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        st.error(f"構造化サマリー生成エラー: {e}")
        return None


# ═══════════════════════════════════════════
# 構造化サマリー → HTML
# ═══════════════════════════════════════════
def summary_to_html(data, file_labels, generated_at):
    urgency_color = {"高": "#e53e5a", "中": "#f5a623", "低": "#22c38e"}.get(data.get("urgency", "中"), "#888")
    urgency_bg    = {"高": "#fff0f2", "中": "#fff8ee", "低": "#f0fff8"}.get(data.get("urgency", "中"), "#f5f5f5")

    flow_items = data.get("flow", [])
    flow_html = ""
    for i, f in enumerate(flow_items):
        connector = '<div class="flow-arrow">↓</div>' if i < len(flow_items) - 1 else ""
        flow_html += f"""
        <div class="flow-item">
          <div class="flow-time">{f.get('time','')}</div>
          <div class="flow-content">
            <div class="flow-topic">{f.get('topic','')}</div>
            <div class="flow-summary">{f.get('summary','')}</div>
          </div>
        </div>{connector}"""

    decisions = data.get("decisions", [])
    dec_html = "".join(
        f'<div class="card-item card-decision"><div class="card-item-title">✅ {d.get("title","")}</div><div class="card-item-detail">{d.get("detail","")}</div></div>'
        for d in decisions
    ) if decisions else '<div class="empty-note">言及なし</div>'

    actions = data.get("actions", [])
    pc_map = {"高": "#e53e5a", "中": "#f5a623", "低": "#22c38e"}
    act_html = "".join(
        f'''<div class="action-row">
          <span class="action-priority" style="background:{pc_map.get(a.get("priority","中"),"#888")}20;color:{pc_map.get(a.get("priority","中"),"#888")};border:1px solid {pc_map.get(a.get("priority","中"),"#888")}40">{a.get("priority","")}</span>
          <div class="action-body">
            <div class="action-what">{a.get("what","")}</div>
            <div class="action-meta">👤 {a.get("who","未定")} &nbsp;｜&nbsp; 📅 {a.get("when","期限未定")}</div>
          </div>
        </div>'''
        for a in sorted(actions, key=lambda x: {"高":0,"中":1,"低":2}.get(x.get("priority","中"),1))
    ) if actions else '<div class="empty-note">言及なし</div>'

    concerns = data.get("concerns", [])
    con_html = "".join(
        f'<div class="card-item card-concern"><div class="card-item-title">⚠️ {c.get("title","")}</div><div class="card-item-detail">{c.get("detail","")}</div></div>'
        for c in concerns
    ) if concerns else '<div class="empty-note">言及なし</div>'

    nexts = data.get("next_topics", [])
    next_html = "".join(f"<li>{n}</li>" for n in nexts) if nexts else '<li class="empty-note">言及なし</li>'

    nums = data.get("key_numbers", [])
    num_html = "".join(
        f'<div class="kpi-card"><div class="kpi-value">{n.get("value","")}</div><div class="kpi-label">{n.get("label","")}</div></div>'
        for n in nums
    )

    keywords = data.get("keywords", [])
    kw_html = "".join(f'<span class="keyword">{k}</span>' for k in keywords)
    participants = data.get("participants", [])
    par_html = "・".join(participants) if participants else "不明"
    files_html = "・".join(file_labels)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{data.get('title','構造化サマリー')}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@300;400;500;700;900&display=swap');
:root {{
  --ink:#1a2140;--ink2:#4a567a;--ink3:#8892b0;
  --line:#e2e8f0;--bg:#f8faff;--card:#ffffff;
  --blue:#3b6ef0;--blue-lt:#eef2ff;
  --green:#22c38e;--red:#e53e5a;--amber:#f5a623;
  --radius:10px;--shadow:0 2px 12px rgba(26,33,64,.08);
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'Noto Sans JP',sans-serif;background:var(--bg);color:var(--ink);font-size:14px;line-height:1.7;}}
.page{{max-width:860px;margin:0 auto;padding:40px 32px 80px;}}
.doc-header{{border-bottom:3px solid var(--blue);padding-bottom:24px;margin-bottom:32px;}}
.doc-meta{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;}}
.meta-chip{{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;letter-spacing:.06em;color:var(--ink3);background:white;border:1px solid var(--line);border-radius:99px;padding:3px 11px;}}
.doc-title{{font-size:clamp(20px,3vw,28px);font-weight:900;color:var(--ink);line-height:1.3;margin-bottom:12px;}}
.one-line{{font-size:14px;color:var(--ink2);background:var(--blue-lt);border-left:4px solid var(--blue);padding:10px 16px;border-radius:0 8px 8px 0;font-weight:500;}}
.urgency-badge{{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:700;padding:4px 14px;border-radius:99px;background:{urgency_bg};color:{urgency_color};border:1.5px solid {urgency_color}40;margin-top:12px;}}
.kpi-row{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:32px;}}
.kpi-card{{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px 20px;min-width:120px;text-align:center;box-shadow:var(--shadow);flex:1;}}
.kpi-value{{font-size:22px;font-weight:900;color:var(--blue);line-height:1.2;}}
.kpi-label{{font-size:11px;color:var(--ink3);margin-top:4px;}}
.section{{margin-bottom:32px;}}
.section-title{{font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--blue);margin-bottom:12px;display:flex;align-items:center;gap:8px;}}
.section-title::after{{content:'';flex:1;height:1px;background:var(--line);}}
.flow-wrap{{display:flex;flex-direction:column;}}
.flow-item{{display:flex;gap:16px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 18px;width:100%;box-shadow:var(--shadow);}}
.flow-arrow{{text-align:center;color:var(--ink3);font-size:18px;padding:4px 0;}}
.flow-time{{font-size:11px;font-weight:700;color:var(--blue);background:var(--blue-lt);padding:3px 10px;border-radius:99px;white-space:nowrap;flex-shrink:0;align-self:flex-start;margin-top:2px;}}
.flow-topic{{font-size:14px;font-weight:700;color:var(--ink);margin-bottom:4px;}}
.flow-summary{{font-size:13px;color:var(--ink2);}}
.card-item{{background:var(--card);border-radius:var(--radius);padding:14px 18px;margin-bottom:10px;border:1px solid var(--line);box-shadow:var(--shadow);}}
.card-decision{{border-left:4px solid var(--green);}}
.card-concern{{border-left:4px solid var(--amber);}}
.card-item-title{{font-size:14px;font-weight:700;color:var(--ink);margin-bottom:4px;}}
.card-item-detail{{font-size:13px;color:var(--ink2);}}
.action-row{{display:flex;gap:14px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px;margin-bottom:8px;box-shadow:var(--shadow);}}
.action-priority{{font-size:11px;font-weight:700;padding:3px 10px;border-radius:99px;white-space:nowrap;flex-shrink:0;}}
.action-what{{font-size:14px;font-weight:600;color:var(--ink);margin-bottom:4px;}}
.action-meta{{font-size:12px;color:var(--ink3);}}
.next-list{{list-style:none;display:flex;flex-direction:column;gap:8px;}}
.next-list li{{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:10px 16px;font-size:13px;color:var(--ink2);box-shadow:var(--shadow);}}
.next-list li::before{{content:"→ ";color:var(--blue);font-weight:700;}}
.keyword-wrap{{display:flex;flex-wrap:wrap;gap:8px;}}
.keyword{{background:var(--blue-lt);color:var(--blue);border:1px solid #c0cef8;border-radius:99px;padding:4px 14px;font-size:12px;font-weight:600;}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:24px;}}
.empty-note{{font-size:13px;color:var(--ink3);font-style:italic;padding:8px 4px;}}
.doc-footer{{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);font-size:11px;color:var(--ink3);}}
.files-note{{font-size:12px;color:var(--ink3);margin-top:4px;}}
@media(max-width:600px){{.page{{padding:24px 16px 60px;}}.two-col{{grid-template-columns:1fr;}}}}
@media print{{body{{background:white;}}.page{{padding:20px;max-width:100%;}}.card-item,.action-row,.flow-item,.next-list li{{-webkit-print-color-adjust:exact;print-color-adjust:exact;break-inside:avoid;}}.section{{break-inside:avoid;}}}}
</style>
</head>
<body>
<div class="page">
  <div class="doc-header">
    <div class="doc-meta">
      <span class="meta-chip">📅 {data.get('date','不明')}</span>
      <span class="meta-chip">🎙️ {data.get('type','会議')}</span>
      <span class="meta-chip">⏱ {data.get('duration','不明')}</span>
      <span class="meta-chip">👥 {par_html}</span>
    </div>
    <div class="doc-title">{data.get('title','構造化サマリー')}</div>
    <div class="one-line">{data.get('one_line','')}</div>
    <div class="urgency-badge">{'🔴' if data.get('urgency')=='高' else '🟡' if data.get('urgency')=='中' else '🟢'} 緊急度：{data.get('urgency','中')}</div>
    <div class="files-note">📁 対象ファイル：{files_html}</div>
  </div>
  {"<div class='kpi-row'>" + num_html + "</div>" if nums else ""}
  <div class="section">
    <div class="section-title">📋 話の流れ・構成</div>
    <div class="flow-wrap">{flow_html}</div>
  </div>
  <div class="two-col">
    <div class="section">
      <div class="section-title">✅ 決定事項</div>{dec_html}
    </div>
    <div class="section">
      <div class="section-title">⚠️ 懸念・リスク</div>{con_html}
    </div>
  </div>
  <div class="section">
    <div class="section-title">🎯 アクションアイテム</div>{act_html}
  </div>
  <div class="section">
    <div class="section-title">🔄 次回以降の検討事項</div>
    <ul class="next-list">{next_html}</ul>
  </div>
  {"<div class='section'><div class='section-title'>🏷 キーワード</div><div class='keyword-wrap'>" + kw_html + "</div></div>" if keywords else ""}
  <div class="doc-footer">
    <div>📁 {files_html}</div>
    <div>🕐 生成日時：{generated_at}</div>
  </div>
</div>
</body>
</html>"""


# ═══════════════════════════════════════════
# UI：サイドバー
# ═══════════════════════════════════════════
with st.sidebar:
    st.header("⚙️ 設定")
    if st.session_state.api_key:
        st.success("✓ OpenAI APIキー設定済み")
    else:
        api_key_input = st.text_input(
            "OpenAI APIキー", type="password",
            placeholder="sk-...",
            help="環境変数 OPENAI_API_KEY が未設定の場合に入力"
        )
        if api_key_input:
            st.session_state.api_key = api_key_input
            st.success("✓ APIキー設定済み")

    if NOTION_API_KEY:
        st.success("✓ Notion APIキー設定済み")
    else:
        st.warning("⚠️ NOTION_API_KEY 未設定（Notion保存不可）")

    st.divider()
    st.markdown("""
### 📋 入力ソース
| | 対応 |
|---|---|
| 🎵 音声 | MP3/WAV/M4A/WebM |
| 🎬 YouTube | URLから字幕取得 |
| 📝 テキスト | TXT/MD直接入力 |

**📄 補足資料（任意）**
PDF / PPTX / DOCX

### 📤 出力
- PLAUDレポート（.md）
- マインドマップ（Markmap）
- Notion研修DB保存

### ⚙️ Railway環境変数設定
```
NOTION_API_KEY=secret_xxx
NOTION_DB_ID_KENSHU=475162c3cf1f4993a9b231e202ec40fb
```
`railway variables set` コマンドで追加してください。
""")


# ═══════════════════════════════════════════
# UI：メイン
# ═══════════════════════════════════════════
st.title("🎙️ 音声メモアプリ Pro")
st.caption("音声 / YouTube / テキスト → PLAUDレポート ／ マインドマップ ／ Notion保存")

if not st.session_state.api_key:
    st.warning("⚠️ OpenAI APIキーが未設定です。環境変数 OPENAI_API_KEY を設定するか、サイドバーで入力してください。")
    st.stop()

# ── 入力ソース選択 ──
st.subheader("① 入力ソースを選択")
source_type = st.radio(
    "入力ソース",
    ["🎵 音声ファイル", "🎬 YouTube URL", "📝 テキストファイル"],
    horizontal=True,
    label_visibility="collapsed",
)

audio_files = []
youtube_url = ""
text_files = []
pasted_text = ""

if source_type == "🎵 音声ファイル":
    st.caption("複数ファイルはファイル名順で結合し、**1つのレポート**を作成します。")
    audio_files = st.file_uploader(
        "MP3・WAV・M4A・WebM",
        type=["mp3", "wav", "m4a", "webm"],
        accept_multiple_files=True,
    )

elif source_type == "🎬 YouTube URL":
    st.caption("公開動画で字幕（自動生成を含む）が有効なものに対応します。")
    youtube_url = st.text_input(
        "YouTube URL", placeholder="https://www.youtube.com/watch?v=..."
    )

elif source_type == "📝 テキストファイル":
    st.caption("TXT / MD ファイルをアップロード、またはテキストを直接貼り付けてください。Whisperをスキップして直接レポート生成します。")
    text_files = st.file_uploader(
        "TXT・MD（ファイルアップロード）",
        type=["txt", "md"],
        accept_multiple_files=True,
    )
    st.caption("または")
    pasted_text = st.text_area(
        "テキスト直接貼り付け",
        height=300,
        placeholder="文字起こしテキストをここに貼り付けてください...",
        key="pasted_text_input",
        label_visibility="collapsed",
    )

st.markdown("---")

# ── 補足資料 ──
st.subheader("② 補足資料（任意・複数可）")
st.caption("会議資料・スライドなど。なくても動作します。")
material_files = st.file_uploader(
    "PDF・PPTX・DOCX",
    type=["pdf", "pptx", "ppt", "docx", "doc"],
    accept_multiple_files=True,
    key="material_uploader",
)

st.markdown("---")

# ── 添付資料（Notion保存用）──
st.subheader("③ 添付資料（Notion保存用・任意・複数可）")
st.caption("Notionページに添付ファイル一覧として記録します（ファイル名と容量のみ保存）。")
attachment_files = st.file_uploader(
    "PDF・PPTX・DOCX",
    type=["pdf", "pptx", "ppt", "docx", "doc"],
    accept_multiple_files=True,
    key="attachment_uploader",
)

# ── 入力確認表示 ──
has_input = bool(audio_files or youtube_url.strip() or text_files or pasted_text.strip())

if has_input:
    if audio_files:
        def sort_key(f):
            nums = re.findall(r'\d+', f.name)
            return "".join(nums).zfill(20) if nums else f.name
        sorted_audio = sorted(audio_files, key=sort_key)

        with st.expander(f"📋 音声ファイル {len(sorted_audio)}件", expanded=True):
            for i, f in enumerate(sorted_audio, 1):
                mb = f.size / (1024 * 1024)
                c1, c2, c3 = st.columns([5, 2, 2])
                c1.write(f"**{i}.** {f.name}")
                c2.caption(f"{mb:.1f} MB")
                c3.caption("🔧 要圧縮" if mb > 24 else "✅ OK")
            if len(sorted_audio) > 1:
                st.info(f"💡 {len(sorted_audio)}件を順番に文字起こし → 結合 → 1本のレポートを生成します。")
    elif youtube_url.strip():
        st.info(f"🎬 YouTube: {youtube_url.strip()}")
    elif pasted_text.strip():
        preview = pasted_text.strip()[:80].replace("\n", " ")
        st.info(f"📝 貼り付けテキスト（{len(pasted_text.strip()):,}文字）: {preview}...")
    elif text_files:
        st.info(f"📝 テキストファイル: {', '.join(f.name for f in text_files)}")

    if material_files:
        st.info(f"📎 補足資料（{len(material_files)}件）: {', '.join(f.name for f in material_files)}")

    st.markdown("---")
    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("🗑️ 処理結果をクリア", use_container_width=True):
            st.session_state.results = []
            st.rerun()
    with btn_col2:
        run = st.button("🚀 処理開始", type="primary", use_container_width=True)

    if run:
        # ── 補足資料抽出 ──
        combined_material = None
        if material_files:
            with st.spinner("📄 補足資料を読み込み中..."):
                mat_texts = []
                for mf in material_files:
                    t = extract_material_text(mf)
                    if t and not t.startswith("["):
                        mat_texts.append(f"=== {mf.name} ===\n{t}")
                    else:
                        st.warning(f"⚠️ {mf.name}: {t}")
            if mat_texts:
                combined_material = "\n\n".join(mat_texts)
                st.success(f"✅ 資料 {len(mat_texts)}件 読み込み完了")

        # ── STEP 1：テキスト取得 ──
        st.markdown("---")
        raw_transcript = ""
        file_labels = []
        transcripts_per_file = {}
        source_label = "音声"  # Notion保存用

        if source_type == "🎵 音声ファイル":
            source_label = "音声"
            st.markdown("### 🎧 STEP1：文字起こし")
            all_tmp_paths = []
            for idx, audio_file in enumerate(sorted_audio):
                st.markdown(f"**[{idx+1}/{len(sorted_audio)}]** {audio_file.name}")
                suffix = Path(audio_file.name).suffix
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                    f.write(audio_file.read())
                    tmp_path = f.name
                    all_tmp_paths.append(tmp_path)
                with st.spinner("  文字起こし中..."):
                    tr = transcribe_audio(tmp_path, st.session_state.api_key)
                if tr:
                    transcripts_per_file[audio_file.name] = tr
                    st.success(f"  ✅ 完了（{len(tr):,}文字）")
                else:
                    st.error("  ❌ 失敗")
            for p in all_tmp_paths:
                if os.path.exists(p):
                    os.remove(p)
            if not transcripts_per_file:
                st.error("文字起こしに成功したファイルがありません。")
                st.stop()
            file_labels = list(transcripts_per_file.keys())
            raw_transcript = (
                list(transcripts_per_file.values())[0]
                if len(file_labels) == 1
                else "\n\n".join(f"--- {k} ---\n{v}" for k, v in transcripts_per_file.items())
            )
            st.success(f"✅ 文字起こし完了（合計 {len(raw_transcript):,}文字）")

        elif source_type == "🎬 YouTube URL":
            source_label = "YouTube"
            st.markdown("### 🎬 STEP1：YouTube字幕取得")
            with st.spinner("字幕を取得中..."):
                raw_transcript, video_id = get_youtube_transcript(youtube_url.strip())
            if not raw_transcript:
                st.error(f"❌ 取得失敗")
                # video_id には失敗理由が入っている
                for line in video_id.splitlines():
                    st.error(line)
                st.info(
                    "💡 **対処法**\n\n"
                    "**① 字幕をコピーして貼り付け（最も簡単）**\n"
                    "  1. YouTube動画を開き、動画下の「…」→「文字起こし」をクリック\n"
                    "  2. 表示されたテキストを全選択してコピー\n"
                    "  3. 入力ソースを **「📝 テキストファイル」** に切り替えてテキスト欄に貼り付け\n\n"
                    "**② yt-dlp フォールバック（音声からWhisper文字起こし）**\n"
                    "  - `pip install yt-dlp` でインストールすると字幕なし動画にも対応します\n\n"
                    "**③ その他の原因**\n"
                    "  - IP ブロック → しばらく待つか別のネットワークで試してください\n"
                    "  - 年齢制限 → 現時点では取得不可（API 制限）\n"
                    "  - URL フォーマット → `watch?v=` / `youtu.be/` / Shorts URL に対応しています"
                )
                st.stop()
            file_labels = [youtube_url.strip()]
            transcripts_per_file = {youtube_url.strip(): raw_transcript}
            st.success(f"✅ 字幕取得完了（{len(raw_transcript):,}文字）")

        elif source_type == "📝 テキストファイル":
            source_label = "テキスト"
            st.markdown("### 📝 STEP1：テキスト読み込み")
            # 貼り付けテキストを優先。なければファイルを読み込む
            if pasted_text.strip():
                content = pasted_text.strip()
                transcripts_per_file["貼り付けテキスト"] = content
                file_labels.append("貼り付けテキスト")
                st.success(f"✅ 貼り付けテキスト（{len(content):,}文字）")
                if text_files:
                    st.info("💡 ファイルと貼り付けテキストの両方が入力されています。貼り付けテキストを優先して処理します。")
            else:
                for tf in text_files:
                    content = tf.read().decode("utf-8", errors="replace")
                    transcripts_per_file[tf.name] = content
                    file_labels.append(tf.name)
                    st.success(f"✅ {tf.name}（{len(content):,}文字）")
            if not transcripts_per_file:
                st.error("テキストが入力されていません。")
                st.stop()
            raw_transcript = "\n\n".join(
                f"--- {k} ---\n{v}" for k, v in transcripts_per_file.items()
            ) if len(transcripts_per_file) > 1 else list(transcripts_per_file.values())[0]

        # ── 長文圧縮 ──
        if len(raw_transcript) > MAX_TRANSCRIPT_CHARS:
            st.info(f"📝 テキストが長いため圧縮します（{len(raw_transcript):,}文字）...")
            with st.spinner("圧縮中..."):
                combined_transcript = compress_transcript(raw_transcript, st.session_state.api_key)
        else:
            combined_transcript = raw_transcript

        # ── STEP 2：PLAUDレポート生成 ──
        st.markdown("### 📊 STEP2：PLAUDレポート生成")
        with st.spinner("GPT-4o でレポート生成中..."):
            report = generate_report(
                combined_transcript, file_labels, combined_material, st.session_state.api_key
            )
        if not report:
            st.error("レポート生成に失敗しました。")
            st.stop()
        st.success(f"✅ レポート完了{'（資料補完あり）' if combined_material else ''}")

        # ── STEP 3：Markmap生成 ──
        st.markdown("### 🗺️ STEP3：マインドマップ生成")
        with st.spinner("Markmap生成中..."):
            markmap_md = generate_markmap(report, st.session_state.api_key)
        if markmap_md:
            st.success("✅ マインドマップ生成完了")
        else:
            st.warning("⚠️ マインドマップ生成に失敗しました")

        # ── STEP 4：構造化サマリー生成 ──
        st.markdown("### 📋 STEP4：構造化サマリー生成")
        with st.spinner("構造化サマリー生成中..."):
            summary_data = generate_summary_json(
                combined_transcript, report, combined_material, st.session_state.api_key
            )
        summary_html = None
        if summary_data:
            generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            summary_html = summary_to_html(summary_data, file_labels, generated_at)
            st.success("✅ 構造化サマリー完了")

        # 結果保存
        result = {
            "label": "・".join(file_labels),
            "file_labels": file_labels,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "transcripts_per_file": transcripts_per_file,
            "combined_transcript": raw_transcript,
            "report": report,
            "markmap_md": markmap_md,
            "summary_html": summary_html,
            "summary_data": summary_data,
            "has_material": combined_material is not None,
            "source_label": source_label,
            "youtube_url": youtube_url.strip() if source_type == "🎬 YouTube URL" else "",
            "attachment_file_info": [
                {"name": f.name, "size": f.size} for f in (attachment_files or [])
            ],
        }
        st.session_state.results = [result] + st.session_state.results
        st.balloons()
        st.success("🎉 処理完了！")


# ── 結果表示 ──
if st.session_state.results:
    st.markdown("---")
    hcol1, hcol2 = st.columns([4, 1])
    hcol1.header(f"📋 処理結果（{len(st.session_state.results)}件）")
    if hcol2.button("🗑️ 全クリア", key="clear_top"):
        st.session_state.results = []
        st.rerun()

    for result in st.session_state.results:
        mat_badge = "  📎 資料補完あり" if result["has_material"] else ""
        n_files = len(result["file_labels"])
        header_label = (
            f"📁 [{n_files}件統合] {result['label']}  —  {result['date']}{mat_badge}"
            if n_files > 1
            else f"📁 {result['label']}  —  {result['date']}{mat_badge}"
        )

        with st.expander(header_label, expanded=True):
            tab_labels = ["📄 文字起こし", "📊 レポート"]
            if result.get("markmap_md"):
                tab_labels.append("🗺️ マインドマップ")
            if result.get("summary_html"):
                tab_labels.append("📋 構造化サマリー")
            tabs = st.tabs(tab_labels)

            # 文字起こし
            with tabs[0]:
                if n_files > 1:
                    sub_labels = list(result["transcripts_per_file"].keys()) + ["📄 全文（結合）"]
                    sub_tabs = st.tabs(sub_labels)
                    for i, (fname, tr) in enumerate(result["transcripts_per_file"].items()):
                        with sub_tabs[i]:
                            st.text_area("", tr, height=220,
                                         key=f"tr_{result['date']}_{fname}")
                            st.download_button(
                                f"📥 {fname} (.txt)",
                                tr,
                                file_name=f"transcript_{Path(fname).stem}.txt",
                                mime="text/plain",
                                key=f"dtr_{result['date']}_{fname}"
                            )
                    with sub_tabs[-1]:
                        st.text_area("", result["combined_transcript"], height=300,
                                     key=f"tr_all_{result['date']}")
                        st.download_button(
                            "📥 全文字起こし（結合）(.txt)",
                            result["combined_transcript"],
                            file_name=f"transcript_combined_{result['date'].replace(':','').replace(' ','_')}.txt",
                            mime="text/plain",
                            key=f"dtr_all_{result['date']}"
                        )
                else:
                    fname = result["file_labels"][0]
                    tr = result["transcripts_per_file"][fname]
                    st.text_area("", tr, height=250,
                                 key=f"tr_{result['date']}_{fname}")
                    st.download_button(
                        "📥 文字起こし (.txt)",
                        tr,
                        file_name=f"transcript_{Path(fname).stem}.txt",
                        mime="text/plain",
                        key=f"dtr_{result['date']}_{fname}"
                    )

            # レポート
            with tabs[1]:
                if result["report"]:
                    st.markdown(result["report"])
                    fname_base = (
                        Path(result["file_labels"][0]).stem if n_files == 1
                        else f"combined_{result['date'].replace(':','').replace(' ','_')}"
                    )
                    dl_col, notion_col = st.columns([1, 1])
                    with dl_col:
                        st.download_button(
                            "📥 レポート (.md)",
                            result["report"],
                            file_name=f"report_{fname_base}.md",
                            mime="text/markdown",
                            key=f"drp_{result['date']}",
                        )
                    with notion_col:
                        if st.button("☁️ Notionに保存", key=f"notion_{result['date']}",
                                     disabled=not NOTION_API_KEY):
                            title = extract_title_from_report(result["report"])
                            tags  = extract_tags_from_report(result["report"])
                            summary_text = "\n".join(result["report"].splitlines()[:10])
                            # source_info: ファイル名またはURL一覧
                            src_info = result.get("file_labels", [])
                            if result.get("youtube_url"):
                                src_info = [result["youtube_url"]]
                            save_to_notion_kenshu(
                                title=title,
                                tags=tags,
                                source_type=result.get("source_label", "音声"),
                                report=result["report"],
                                summary=summary_text,
                                transcript=result.get("combined_transcript", ""),
                                markmap_md=result.get("markmap_md", ""),
                                summary_data=result.get("summary_data"),
                                source_info=src_info,
                                attachment_file_info=result.get("attachment_file_info", []),
                            )
                    if not NOTION_API_KEY:
                        st.caption("⚠️ NOTION_API_KEY 未設定のため保存不可")

            # マインドマップ
            tab_idx = 2
            if result.get("markmap_md"):
                with tabs[tab_idx]:
                    st.caption("💡 マウスホイールでズーム、ドラッグで移動できます。")
                    mm_html = render_markmap_html(result["markmap_md"])
                    st.components.v1.html(mm_html, height=560, scrolling=False)
                    st.download_button(
                        "📥 Markmap (.md)",
                        result["markmap_md"],
                        file_name=f"markmap_{fname_base}.md",
                        mime="text/markdown",
                        key=f"dmm_{result['date']}",
                    )
                tab_idx += 1

            # 構造化サマリー
            if result.get("summary_html") and len(tabs) > tab_idx:
                with tabs[tab_idx]:
                    st.info("💡 HTMLをダウンロードしてブラウザで開くと、見やすく印刷・PDF化できます。")
                    fname_base = (
                        Path(result["file_labels"][0]).stem if n_files == 1
                        else f"combined_{result['date'].replace(':','').replace(' ','_')}"
                    )
                    st.download_button(
                        "📥 構造化サマリー (.html)",
                        result["summary_html"],
                        file_name=f"summary_{fname_base}.html",
                        mime="text/html",
                        key=f"dsum_{result['date']}",
                    )
                    with st.expander("🔍 プレビュー（アプリ内）"):
                        st.components.v1.html(result["summary_html"], height=800, scrolling=True)

st.markdown("---")
st.caption("🎙️ 音声メモアプリ Pro ／ Powered by OpenAI Whisper & GPT-4o")
