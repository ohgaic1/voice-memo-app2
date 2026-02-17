import streamlit as st
import tempfile
import os
import re
import json
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

if "api_key" not in st.session_state:
    st.session_state.api_key = ""
if "results" not in st.session_state:
    st.session_state.results = []


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

    prompt = f"""以下の音声文字起こしから詳細な構造化レポートを作成してください。
{files_note}
{safe_material}
【文字起こし】
{safe_transcript}

# 📝 エグゼクティブサマリー
（核心を捉えた2〜3段落。最重要な洞察・結論を含める）

# 🎯 キーポイント
（5〜10個の具体的な重要ポイント。各ポイントは文脈を含める）

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
- 音声ファイル数: {len(file_labels)}件
- 対象ファイル: {', '.join(file_labels)}
- 補足資料: {"あり（内容を反映済み）" if material_text else "なし"}
- 緊急度: [高/中/低]

※文字起こしにない情報は「言及なし」と記載。"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは音声メモから高品質な構造化レポートを作成する専門家です。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=3000
        )
        return resp.choices[0].message.content
    except Exception as e:
        error_str = str(e)
        if "rate_limit_exceeded" in error_str or "too large" in error_str.lower():
            st.warning("⚠️ テキストが長すぎるため、さらに短縮して再試行します...")
            # フォールバック：さらに短縮
            short_transcript = combined_transcript[:6000]
            try:
                resp2 = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "あなたは音声メモから高品質な構造化レポートを作成する専門家です。"},
                        {"role": "user", "content": prompt.replace(safe_transcript, short_transcript)}
                    ],
                    temperature=0.3,
                    max_tokens=3000
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

### 📤 出力ファイル
| | 内容 |
|---|---|
| .txt | 文字起こし |
| .md | 詳細レポート |
| .html | 構造化サマリー |

### ⚙️ トークン制限対策
長い文字起こしは自動で調整して送信します。
""")


# ═══════════════════════════════════════════
# UI：メイン
# ═══════════════════════════════════════════
st.title("🎙️ 音声メモアプリ Pro")
st.caption("複数ファイル → 統合レポート ／ PDF・PPTX補完 ／ Plaud風レポート ／ 構造化サマリー")

if not st.session_state.api_key:
    st.warning("⚠️ サイドバーでOpenAI APIキーを設定してください")
    st.stop()

# ── アップロードエリア（縦並び） ──
st.subheader("🎵 音声ファイル（複数選択可）")
st.caption("複数ファイルはファイル名順（作成日時順）で結合し、**1つのレポート**を作成します。")
audio_files = st.file_uploader(
    "MP3・WAV・M4A・WebM",
    type=["mp3", "wav", "m4a", "webm"],
    accept_multiple_files=True
)

st.markdown("---")

st.subheader("📄 補足資料（任意・複数可）")
st.caption("会議資料・スライドなど。**なくても動作します。**")
material_files = st.file_uploader(
    "PDF・PPTX・DOCX",
    type=["pdf", "pptx", "ppt", "docx", "doc"],
    accept_multiple_files=True
)

# ── ファイル確認表示 ──
if audio_files:
    def sort_key(f):
        nums = re.findall(r'\d+', f.name)
        return "".join(nums).zfill(20) if nums else f.name

    sorted_audio = sorted(audio_files, key=sort_key)

    st.markdown("---")
    with st.expander(
        f"📋 処理予定：音声 {len(sorted_audio)}件（結合して1つのレポートを生成）",
        expanded=True
    ):
        for i, f in enumerate(sorted_audio, 1):
            mb = f.size / (1024 * 1024)
            c1, c2, c3 = st.columns([5, 2, 2])
            c1.write(f"**{i}.** {f.name}")
            c2.caption(f"{mb:.1f} MB")
            c3.caption("🔧 要圧縮" if mb > 24 else "✅ OK")
        total_mb = sum(f.size for f in sorted_audio) / (1024 * 1024)
        st.caption(f"合計：{total_mb:.1f} MB")
        if len(sorted_audio) > 1:
            st.info(f"💡 {len(sorted_audio)}件を順番に文字起こし → テキスト結合 → 1本のレポートを生成します。")

    if material_files:
        st.info(f"📎 補足資料（{len(material_files)}件）: {', '.join(f.name for f in material_files)}")
    else:
        st.caption("📎 補足資料なし")

    st.markdown("---")
    btn_col1, btn_col2 = st.columns(2)

    with btn_col1:
        if st.button("🗑️ 処理結果をクリア", use_container_width=True):
            st.session_state.results = []
            st.success("✅ クリアしました。")
            st.rerun()

    with btn_col2:
        run = st.button("🚀 処理開始", type="primary", use_container_width=True)

    if run:
        # 資料抽出
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

        # ── ステップ1：文字起こし ──
        st.markdown("---")
        st.markdown("### 🎧 ステップ1：文字起こし")

        transcripts_per_file = {}
        all_tmp_paths = []

        for idx, audio_file in enumerate(sorted_audio):
            st.markdown(f"**[{idx+1}/{len(sorted_audio)}]** {audio_file.name}")
            suffix = Path(audio_file.name).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                f.write(audio_file.read())
                tmp_path = f.name
                all_tmp_paths.append(tmp_path)

            with st.spinner("  文字起こし中..."):
                transcript = transcribe_audio(tmp_path, st.session_state.api_key)

            if transcript:
                transcripts_per_file[audio_file.name] = transcript
                st.success(f"  ✅ 完了（{len(transcript):,}文字）")
            else:
                st.error(f"  ❌ 失敗")

        for p in all_tmp_paths:
            if os.path.exists(p):
                os.remove(p)

        if not transcripts_per_file:
            st.error("文字起こしに成功したファイルがありません。")
            st.stop()

        file_labels = list(transcripts_per_file.keys())

        # テキスト結合
        if len(file_labels) == 1:
            raw_transcript = list(transcripts_per_file.values())[0]
        else:
            parts = []
            for fname, tr in transcripts_per_file.items():
                parts.append(f"--- {fname} ---\n{tr}")
            raw_transcript = "\n\n".join(parts)

        total_chars = len(raw_transcript)
        st.success(f"✅ 文字起こし完了（合計 {total_chars:,}文字）")

        # ── トークン制限対策：長い場合は事前圧縮 ──
        if total_chars > MAX_TRANSCRIPT_CHARS:
            st.info(f"📝 テキストが長いため（{total_chars:,}文字）、要点を圧縮してからレポートを生成します...")
            with st.spinner("圧縮中..."):
                combined_transcript = compress_transcript(raw_transcript, st.session_state.api_key)
        else:
            combined_transcript = raw_transcript

        # ── ステップ2：レポート生成 ──
        st.markdown("### 📊 ステップ2：統合レポート生成")
        with st.spinner("GPT-4o でレポート生成中..."):
            report = generate_report(
                combined_transcript, file_labels, combined_material, st.session_state.api_key
            )

        if not report:
            st.error("レポート生成に失敗しました。")
            st.stop()

        st.success(f"✅ レポート完了{'（資料補完あり）' if combined_material else ''}")

        # ── ステップ3：構造化サマリー生成 ──
        st.markdown("### 📋 ステップ3：構造化サマリー生成")
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
            "combined_transcript": raw_transcript,   # 元のフル文字起こしを保存
            "report": report,
            "summary_html": summary_html,
            "has_material": combined_material is not None
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
                    st.download_button(
                        "📥 レポート (.md)",
                        result["report"],
                        file_name=f"report_{fname_base}.md",
                        mime="text/markdown",
                        key=f"drp_{result['date']}"
                    )

            # 構造化サマリー
            if result.get("summary_html") and len(tabs) > 2:
                with tabs[2]:
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
                        key=f"dsum_{result['date']}"
                    )
                    with st.expander("🔍 プレビュー（アプリ内）"):
                        st.components.v1.html(result["summary_html"], height=800, scrolling=True)

st.markdown("---")
st.caption("🎙️ 音声メモアプリ Pro ／ Powered by OpenAI Whisper & GPT-4o")
