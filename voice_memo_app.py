import streamlit as st
import tempfile
import os
import re
from pathlib import Path
from datetime import datetime
import subprocess
from openai import OpenAI

# ─────────────────────────────────────────
# ページ設定
# ─────────────────────────────────────────
st.set_page_config(
    page_title="音声メモアプリ Pro",
    page_icon="🎙️",
    layout="wide"
)

# ─────────────────────────────────────────
# セッション初期化
# ─────────────────────────────────────────
if "api_key" not in st.session_state:
    st.session_state.api_key = ""
if "results" not in st.session_state:
    st.session_state.results = []


# ─────────────────────────────────────────
# ユーティリティ：音声処理
# ─────────────────────────────────────────
def compress_audio(input_path, output_path):
    """ffmpegでモノラル16kHz圧縮"""
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
    """10分単位で分割"""
    chunks = []
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True, check=True
        )
        duration = float(probe.stdout.strip())
        num = int(duration / chunk_sec) + 1
        for i in range(num):
            out = input_path.replace(".mp3", f"_chunk{i}.mp3")
            subprocess.run(
                ["ffmpeg", "-i", input_path,
                 "-ss", str(i * chunk_sec), "-t", str(chunk_sec),
                 "-c", "copy", "-y", out],
                check=True, capture_output=True
            )
            if os.path.exists(out) and os.path.getsize(out) > 1000:
                chunks.append(out)
    except Exception as e:
        st.error(f"分割エラー: {e}")
    return chunks


def transcribe_audio(file_path, api_key):
    """Whisper APIで文字起こし（大容量対応）"""
    client = OpenAI(api_key=api_key)
    max_size = 24 * 1024 * 1024

    try:
        size = os.path.getsize(file_path)
        work_path = file_path

        # 圧縮が必要な場合
        if size > max_size:
            st.info("🔧 ファイルを圧縮中...")
            comp = file_path.replace(Path(file_path).suffix, "_comp.mp3")
            if not compress_audio(file_path, comp):
                return None
            work_path = comp

        # まだ大きければ分割
        if os.path.getsize(work_path) > max_size:
            st.info("✂️ ファイルを分割中...")
            chunks = split_audio(work_path)
            if not chunks:
                return None
            texts = []
            pb = st.progress(0)
            for i, chunk in enumerate(chunks):
                with open(chunk, "rb") as f:
                    r = client.audio.transcriptions.create(
                        model="whisper-1", file=f, language="ja"
                    )
                    texts.append(r.text)
                pb.progress((i + 1) / len(chunks))
                os.remove(chunk)
            if work_path != file_path and os.path.exists(work_path):
                os.remove(work_path)
            return " ".join(texts).strip()

        # 通常処理
        with open(work_path, "rb") as f:
            r = client.audio.transcriptions.create(
                model="whisper-1", file=f, language="ja"
            )
        if work_path != file_path and os.path.exists(work_path):
            os.remove(work_path)
        return r.text

    except Exception as e:
        st.error(f"文字起こしエラー: {e}")
        return None


# ─────────────────────────────────────────
# ユーティリティ：資料テキスト抽出
# ─────────────────────────────────────────
def extract_pdf_text(file_path):
    """PDFからテキスト抽出"""
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
    """PPTXからテキスト抽出"""
    try:
        from pptx import Presentation
        prs = Presentation(file_path)
        texts = []
        for i, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_texts.append(shape.text.strip())
            if slide_texts:
                texts.append(f"【スライド{i}】\n" + "\n".join(slide_texts))
        return "\n\n".join(texts)
    except Exception as e:
        return f"[PPTX読み取りエラー: {e}]"


def extract_docx_text(file_path):
    """DOCXからテキスト抽出"""
    try:
        from docx import Document
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        return f"[DOCX読み取りエラー: {e}]"


def extract_material_text(uploaded_file):
    """アップロードされた資料ファイルからテキストを抽出"""
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
        else:
            return f"[未対応形式: {suffix}]"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ─────────────────────────────────────────
# GPT：Plaud風レポート生成
# ─────────────────────────────────────────
def generate_report(transcript, material_text, api_key):
    client = OpenAI(api_key=api_key)

    material_section = ""
    if material_text and material_text.strip():
        material_section = f"""
---
【補足資料の内容】
{material_text[:4000]}
---
上記の資料内容も踏まえて、より正確・詳細にレポートを作成してください。
資料に記載された具体的な数値・固有名詞・用語を積極的に活用してください。
"""

    prompt = f"""以下の音声文字起こしから、詳細で構造化されたレポートを作成してください。
{material_section}

【文字起こし】
{transcript}

以下の形式で詳細にレポートを作成してください：

# 📝 エグゼクティブサマリー
（核心を捉えた2〜3段落の要約。最重要な洞察・結論を含める）

# 🎯 キーポイント
（5〜10個の具体的な重要ポイント。箇条書き。各ポイントは文脈を含める）

# 💡 主要な洞察と分析
（3〜5個の深い洞察。なぜ重要か・どんな意味があるかを説明）

# ✅ アクションアイテム
（実行可能な具体的タスクを優先度付きで列挙。誰が・何を・いつまでに）

# 🗣️ 重要な発言・引用
（特に重要な発言を3〜5個抜粋。文脈と共に）

# 📊 トピック別詳細分析
（主要トピックごとに詳しく分析。決定事項・懸念点など）

# 🔄 フォローアップ事項
（今後の確認事項・未解決の問題・次のステップ）

# 📌 メタ情報
- 推定所要時間: [X分]
- 主要参加者/話者: [推定]
- 会議/メモのタイプ: [推定]
- 緊急度: [高/中/低]
{"- 補足資料: あり（内容を反映済み）" if material_text else "- 補足資料: なし"}

※ 文字起こしに含まれない情報は推測せず「言及なし」と記載。
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは音声メモから高品質な構造化レポートを作成する専門家です。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        return resp.choices[0].message.content
    except Exception as e:
        st.error(f"レポート生成エラー: {e}")
        return None


# ─────────────────────────────────────────
# GPT：マインドマップ生成
# ─────────────────────────────────────────
def generate_mindmap(transcript, report, material_text, api_key):
    client = OpenAI(api_key=api_key)

    material_hint = "\n補足資料の概要も含めてください。" if material_text else ""

    prompt = f"""以下の音声文字起こしとレポートから、マインドマップをMarkdown形式で作成してください。{material_hint}

【文字起こし（抜粋）】
{transcript[:2000]}

【レポート】
{report[:3000]}

以下のルールに従ってください：
- ルートノードは会議・メモのテーマ
- 第1階層：主要トピック（4〜7個）
- 第2階層：各トピックの詳細（2〜5個）
- 第3階層：さらに具体的な内容（必要な場合のみ）
- Markdownのインデントで階層を表現
- 各項目は短く・具体的に（15文字以内推奨）
- アクションアイテムは【ACTION】プレフィックスを付ける
- 重要事項は【重要】プレフィックスを付ける

出力形式（例）：
# 🧠 [テーマ名]

## 📌 [トピック1]
  - [詳細1-1]
    - [詳細1-1-1]
  - [詳細1-2]

## 📌 [トピック2]
  - [詳細2-1]
  - 【ACTION】[アクション項目]

## ✅ アクションまとめ
  - 【ACTION】[重要なアクション1]
  - 【ACTION】[重要なアクション2]

このフォーマットで実際の内容を埋めて出力してください。
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは情報整理の専門家です。マインドマップを構造的に作成します。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        return resp.choices[0].message.content
    except Exception as e:
        st.error(f"マインドマップ生成エラー: {e}")
        return None


# ─────────────────────────────────────────
# マインドマップ → HTML変換
# ─────────────────────────────────────────
def mindmap_to_html(mindmap_md):
    lines = mindmap_md.strip().split("\n")
    nodes = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith("# "):
            nodes.append({"depth": 0, "text": line[2:].strip(), "type": "root"})
        elif line.startswith("## "):
            nodes.append({"depth": 1, "text": line[3:].strip(), "type": "branch"})
        elif stripped.startswith("- ") or stripped.startswith("* "):
            spaces = len(line) - len(line.lstrip())
            depth = 2 if spaces <= 4 else 3
            text = stripped[2:]
            node_type = "action" if "【ACTION】" in text else ("important" if "【重要】" in text else "leaf")
            text = text.replace("【ACTION】", "").replace("【重要】", "")
            nodes.append({"depth": depth, "text": text, "type": node_type})

    parts = []
    for node in nodes:
        t = node["text"].replace("<", "&lt;").replace(">", "&gt;")
        nt = node["type"]
        d = node["depth"]

        if d == 0:
            parts.append(f'<div class="mm-root">{t}</div>')
        elif d == 1:
            parts.append(f'<div class="mm-branch">{t}</div>')
        elif d == 2:
            cls = f"mm-{nt}" if nt in ["action", "important"] else "mm-leaf"
            parts.append(f'<div class="{cls}">{t}</div>')
        else:
            cls = f"mm-{nt}" if nt in ["action", "important"] else "mm-leaf2"
            parts.append(f'<div class="{cls} mm-deep">{t}</div>')

    body = "\n".join(parts)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>マインドマップ</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap');
  body {{
    font-family: 'Noto Sans JP', sans-serif;
    background: #f0f4ff;
    margin: 0; padding: 32px 24px;
    color: #1a2140;
  }}
  h1 {{ font-size: 16px; color: #4a567a; margin-bottom: 24px; font-weight: 400; }}
  .mm-root {{
    background: linear-gradient(135deg, #2c3e7a, #4e6ef2);
    color: white; border-radius: 14px;
    padding: 16px 24px; font-size: 18px; font-weight: 900;
    margin-bottom: 20px;
    box-shadow: 0 6px 20px rgba(44,62,122,.3);
  }}
  .mm-branch {{
    background: white; border-left: 5px solid #4e6ef2;
    border-radius: 12px; padding: 12px 18px;
    font-size: 15px; font-weight: 700;
    margin: 12px 0 6px 0;
    box-shadow: 0 2px 10px rgba(0,0,0,.08);
  }}
  .mm-leaf {{
    background: #f0f4ff; border-left: 3px solid #a0b0e8;
    border-radius: 8px; padding: 8px 16px;
    font-size: 13.5px; margin: 5px 0 5px 32px;
  }}
  .mm-leaf2, .mm-deep {{
    background: #fafafa; border-left: 2px solid #d0d8f0;
    border-radius: 6px; padding: 6px 14px;
    font-size: 12.5px; margin: 3px 0 3px 60px; color: #4a567a;
  }}
  .mm-action {{
    background: #e8fff4; border-left: 3px solid #22c38e;
    border-radius: 8px; padding: 8px 16px;
    font-size: 13.5px; margin: 5px 0 5px 32px; color: #0a7a52;
    font-weight: 600;
  }}
  .mm-action::before {{ content: "✅ "; }}
  .mm-important {{
    background: #fff8e8; border-left: 3px solid #f5a623;
    border-radius: 8px; padding: 8px 16px;
    font-size: 13.5px; margin: 5px 0 5px 32px; color: #7a4e00;
    font-weight: 600;
  }}
  .mm-important::before {{ content: "⚠️ "; }}
  @media print {{
    body {{ background: white; padding: 16px; }}
    .mm-root {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style>
</head>
<body>
<h1>🧠 マインドマップ — 生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}</h1>
{body}
</body>
</html>"""


# ─────────────────────────────────────────
# UI：サイドバー
# ─────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 設定")
    api_key = st.text_input(
        "OpenAI APIキー", type="password",
        value=st.session_state.api_key
    )
    if api_key:
        st.session_state.api_key = api_key
        st.success("✓ APIキー設定済み")

    st.divider()
    st.markdown("""
### 📋 対応ファイル形式
**🎵 音声（複数可）**
MP3 / WAV / M4A / WebM

**📄 補足資料（任意・複数可）**
PDF / PPTX / DOCX

### ✨ 新機能
- 📅 作成日時順に自動整列
- 📄 PDF・PPTXで内容を補完
- 🧠 マインドマップ自動生成
- 💾 HTML形式で見やすく保存
""")


# ─────────────────────────────────────────
# UI：メイン
# ─────────────────────────────────────────
st.title("🎙️ 音声メモアプリ Pro")
st.caption("複数ファイル対応 ／ PDF・PPTX補完 ／ Plaud風レポート ／ マインドマップ生成")

if not st.session_state.api_key:
    st.warning("⚠️ サイドバーでOpenAI APIキーを設定してください")
    st.stop()

# ─── アップロードエリア ───
col1, col2 = st.columns([3, 2])

with col1:
    st.subheader("🎵 音声ファイル（複数選択可）")
    audio_files = st.file_uploader(
        "MP3・WAV・M4A・WebM",
        type=["mp3", "wav", "m4a", "webm"],
        accept_multiple_files=True,
        help="複数ファイルを選択できます。ファイル名の数字順（作成日時順）に処理されます。"
    )

with col2:
    st.subheader("📄 補足資料（任意）")
    st.caption("会議資料・議事録・スライドなど。なくても動作します。")
    material_files = st.file_uploader(
        "PDF・PPTX・DOCX",
        type=["pdf", "pptx", "ppt", "docx", "doc"],
        accept_multiple_files=True,
        help="資料がある場合はアップロードすると、レポートの精度が上がります。"
    )

# ─── ファイル確認表示 ───
if audio_files:
    # ファイル名の数字部分でソート（作成日時順を想定）
    def sort_key(f):
        nums = re.findall(r'\d+', f.name)
        return "".join(nums).zfill(20) if nums else f.name

    sorted_audio = sorted(audio_files, key=sort_key)

    st.markdown("---")
    with st.expander(f"📋 処理予定：音声ファイル {len(sorted_audio)}件", expanded=True):
        for i, f in enumerate(sorted_audio, 1):
            size_mb = f.size / (1024 * 1024)
            c1, c2, c3 = st.columns([5, 2, 2])
            c1.write(f"**{i}.** {f.name}")
            c2.caption(f"{size_mb:.1f} MB")
            c3.caption("🔧 要圧縮" if size_mb > 24 else "✅ OK")

    if material_files:
        st.info(f"📎 補足資料（{len(material_files)}件）: {', '.join(f.name for f in material_files)}")
    else:
        st.caption("📎 補足資料なし — 音声のみで処理します")

    # ─── オプション ───
    st.markdown("---")
    opt_col1, opt_col2 = st.columns(2)
    do_mindmap = opt_col1.checkbox("🧠 マインドマップを生成する", value=True)
    run = opt_col2.button("🚀 処理開始", type="primary", use_container_width=True)

    if run:
        # ── 資料テキストの事前抽出 ──
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
                st.success(f"✅ 資料 {len(mat_texts)}件を読み込みました")
            else:
                st.warning("資料の読み込みに失敗しました。音声のみで処理します。")

        # ── 各音声ファイルを処理 ──
        new_results = []
        for idx, audio_file in enumerate(sorted_audio):
            st.markdown(f"---")
            progress_label = f"**[{idx+1}/{len(sorted_audio)}] {audio_file.name}**"
            st.markdown(progress_label)

            result = {
                "filename": audio_file.name,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "transcript": None,
                "report": None,
                "mindmap": None,
                "mindmap_html": None,
                "has_material": combined_material is not None
            }

            suffix = Path(audio_file.name).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                f.write(audio_file.read())
                tmp_path = f.name

            try:
                # 文字起こし
                with st.spinner("🎧 文字起こし中..."):
                    transcript = transcribe_audio(tmp_path, st.session_state.api_key)

                if not transcript:
                    st.error(f"❌ {audio_file.name} の文字起こしに失敗しました")
                    continue

                result["transcript"] = transcript
                char_count = len(transcript)
                st.success(f"✅ 文字起こし完了（{char_count:,}文字）")

                # レポート生成
                with st.spinner("📊 レポート生成中 (GPT-4o)..."):
                    report = generate_report(transcript, combined_material, st.session_state.api_key)

                if report:
                    result["report"] = report
                    material_note = "（資料補完あり）" if combined_material else ""
                    st.success(f"✅ レポート生成完了 {material_note}")

                # マインドマップ生成
                if do_mindmap and report:
                    with st.spinner("🧠 マインドマップ生成中..."):
                        mindmap = generate_mindmap(
                            transcript, report, combined_material, st.session_state.api_key
                        )
                    if mindmap:
                        result["mindmap"] = mindmap
                        result["mindmap_html"] = mindmap_to_html(mindmap)
                        st.success("✅ マインドマップ生成完了")

            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            new_results.append(result)

        # 結果を先頭に追加（新しいものが上）
        st.session_state.results = new_results + st.session_state.results
        st.balloons()
        st.success(f"🎉 {len(new_results)}件の処理が完了しました！")


# ─── 結果表示 ───
if st.session_state.results:
    st.markdown("---")
    st.header(f"📋 処理結果（{len(st.session_state.results)}件）")

    for result in st.session_state.results:
        mat_badge = "  📎 資料補完あり" if result["has_material"] else ""
        with st.expander(
            f"📁 {result['filename']}  —  {result['date']}{mat_badge}",
            expanded=True
        ):
            tab_labels = ["📄 文字起こし", "📊 レポート"]
            if result.get("mindmap"):
                tab_labels.append("🧠 マインドマップ")
            tabs = st.tabs(tab_labels)

            # 文字起こしタブ
            with tabs[0]:
                if result["transcript"]:
                    st.text_area("", result["transcript"], height=250,
                                 key=f"tr_{result['filename']}_{result['date']}")
                    st.download_button(
                        "📥 文字起こし (.txt)",
                        result["transcript"],
                        file_name=f"transcript_{Path(result['filename']).stem}.txt",
                        mime="text/plain",
                        key=f"dtr_{result['filename']}_{result['date']}"
                    )

            # レポートタブ
            with tabs[1]:
                if result["report"]:
                    st.markdown(result["report"])
                    st.download_button(
                        "📥 レポート (.md)",
                        result["report"],
                        file_name=f"report_{Path(result['filename']).stem}.md",
                        mime="text/markdown",
                        key=f"drp_{result['filename']}_{result['date']}"
                    )

            # マインドマップタブ
            if result.get("mindmap") and len(tabs) > 2:
                with tabs[2]:
                    st.markdown(result["mindmap"])
                    mm_col1, mm_col2 = st.columns(2)
                    mm_col1.download_button(
                        "📥 Markdown (.md)",
                        result["mindmap"],
                        file_name=f"mindmap_{Path(result['filename']).stem}.md",
                        mime="text/markdown",
                        key=f"dmm_{result['filename']}_{result['date']}"
                    )
                    if result.get("mindmap_html"):
                        mm_col2.download_button(
                            "📥 HTML（見やすい版）",
                            result["mindmap_html"],
                            file_name=f"mindmap_{Path(result['filename']).stem}.html",
                            mime="text/html",
                            key=f"dmmh_{result['filename']}_{result['date']}"
                        )

# ─── フッター ───
st.markdown("---")
st.caption("🎙️ 音声メモアプリ Pro ／ Powered by OpenAI Whisper & GPT-4o")
