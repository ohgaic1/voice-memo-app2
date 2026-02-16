import streamlit as st
import tempfile
import os
from pathlib import Path
import subprocess
from openai import OpenAI
import json
from datetime import datetime

# ページ設定
st.set_page_config(
    page_title="音声メモアプリ（高品質版）",
    page_icon="🎙️",
    layout="wide"
)

# OpenAI APIキーの設定
if "OPENAI_API_KEY" not in st.session_state:
    st.session_state.OPENAI_API_KEY = ""

def compress_audio(input_path, output_path, target_bitrate="32k"):
    """音声ファイルを圧縮"""
    try:
        cmd = [
            "ffmpeg", "-i", input_path,
            "-vn",  # 映像を除外
            "-ac", "1",  # モノラル
            "-ar", "16000",  # サンプリングレート16kHz
            "-b:a", target_bitrate,
            "-y",  # 上書き
            output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        st.error(f"圧縮エラー: {e.stderr.decode()}")
        return False
    except FileNotFoundError:
        st.error("ffmpegがインストールされていません。")
        return False

def split_audio(input_path, chunk_duration=600):
    """音声ファイルを指定秒数ごとに分割（デフォルト10分）"""
    chunks = []
    try:
        # 音声の長さを取得
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        
        # チャンクに分割
        num_chunks = int(duration / chunk_duration) + 1
        
        for i in range(num_chunks):
            start_time = i * chunk_duration
            chunk_path = input_path.replace(".mp3", f"_chunk_{i}.mp3")
            
            split_cmd = [
                "ffmpeg", "-i", input_path,
                "-ss", str(start_time),
                "-t", str(chunk_duration),
                "-c", "copy",
                "-y",
                chunk_path
            ]
            subprocess.run(split_cmd, check=True, capture_output=True)
            chunks.append(chunk_path)
        
        return chunks
    except Exception as e:
        st.error(f"分割エラー: {str(e)}")
        return []

def transcribe_audio(file_path, api_key):
    """OpenAI Whisper APIで文字起こし"""
    client = OpenAI(api_key=api_key)
    
    file_size = os.path.getsize(file_path)
    max_size = 24 * 1024 * 1024  # 24MB (余裕を持って)
    
    try:
        # ファイルサイズチェック
        if file_size > max_size:
            st.info("ファイルが大きいため、圧縮処理を行います...")
            
            # 圧縮
            compressed_path = file_path.replace(".mp3", "_compressed.mp3")
            if not compress_audio(file_path, compressed_path):
                return None
            
            # 圧縮後もサイズオーバーなら分割
            if os.path.getsize(compressed_path) > max_size:
                st.info("圧縮後もファイルが大きいため、分割処理を行います...")
                chunks = split_audio(compressed_path)
                
                if not chunks:
                    return None
                
                # 各チャンクを文字起こし
                full_transcript = ""
                progress_bar = st.progress(0)
                
                for idx, chunk_path in enumerate(chunks):
                    with open(chunk_path, "rb") as audio_file:
                        transcript = client.audio.transcriptions.create(
                            model="whisper-1",
                            file=audio_file,
                            language="ja"
                        )
                        full_transcript += transcript.text + " "
                    
                    progress_bar.progress((idx + 1) / len(chunks))
                    
                    # チャンクファイルを削除
                    os.remove(chunk_path)
                
                os.remove(compressed_path)
                return full_transcript.strip()
            else:
                # 圧縮版を使用
                with open(compressed_path, "rb") as audio_file:
                    transcript = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="ja"
                    )
                os.remove(compressed_path)
                return transcript.text
        else:
            # 通常の文字起こし
            with open(file_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="ja"
                )
                return transcript.text
                
    except Exception as e:
        st.error(f"文字起こしエラー: {str(e)}")
        return None

def generate_plaud_style_report(transcript, api_key):
    """Plaud風の詳細レポートを生成"""
    client = OpenAI(api_key=api_key)
    
    prompt = f"""以下の音声文字起こしテキストから、詳細で構造化されたレポートを作成してください。

文字起こしテキスト:
{transcript}

以下の形式で、できるだけ詳細かつ具体的にレポートを作成してください:

# 📝 エグゼクティブサマリー
（2-3段落で全体の核心を要約。単なる概要ではなく、最も重要な洞察や結論を含める）

# 🎯 キーポイント
（5-10個の重要なポイントを箇条書き。各ポイントは具体的で、文脈を含める）

# 💡 主要な洞察と分析
（3-5個の深い洞察。なぜ重要か、どのような意味を持つかを説明）

# ✅ アクションアイテム
（実行可能な具体的なタスクを優先度付きで列挙。誰が、何を、いつまでにを明確に）

# 🗣️ 重要な発言・引用
（特に印象的または重要な発言を3-5個抜粋。文脈と共に）

# 📊 トピック別詳細分析
（主要なトピックごとに詳しく分析。各トピックで議論された内容、決定事項、懸念点など）

# 🔄 フォローアップ事項
（今後の確認事項、未解決の問題、次のステップ）

# 📌 メタ情報
- 推定所要時間: [X分]
- 主要参加者/話者: [推定]
- 会議/メモのタイプ: [推定：会議、ブレスト、1on1など]
- 緊急度: [高/中/低]

注意事項:
- 文字起こしテキストから具体的な情報を抽出し、想像や一般論は避ける
- 各セクションは詳細に記述し、単なる箇条書きだけでなく説明も加える
- 実際の内容に基づいて、有用で実践的なレポートを作成する
- 文字起こしに含まれない情報は推測せず、「言及なし」と記載する
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは音声メモから高品質な構造化レポートを作成する専門家です。Plaudアプリのような、詳細で実用的なレポートを生成します。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        
        return response.choices[0].message.content
        
    except Exception as e:
        st.error(f"レポート生成エラー: {str(e)}")
        return None

# メインアプリ
st.title("🎙️ 音声メモアプリ（高品質版）")
st.markdown("**大容量対応 + Plaud風詳細レポート生成**")

# サイドバー
with st.sidebar:
    st.header("⚙️ 設定")
    
    api_key = st.text_input(
        "OpenAI APIキー",
        type="password",
        value=st.session_state.OPENAI_API_KEY,
        help="OpenAIのAPIキーを入力してください"
    )
    
    if api_key:
        st.session_state.OPENAI_API_KEY = api_key
        st.success("✓ APIキー設定済み")
    
    st.markdown("---")
    st.markdown("### 📖 使い方")
    st.markdown("""
    1. OpenAI APIキーを入力
    2. 音声ファイルをアップロード
    3. 自動で文字起こし＆レポート生成
    
    **対応形式**: MP3, WAV, M4A, WebM
    
    **特徴**:
    - 🔊 大容量ファイル対応（自動圧縮・分割）
    - 📝 Plaud風の詳細レポート
    - ⚡ GPT-4o使用で高品質
    """)
    
    st.markdown("---")
    st.markdown("### 💡 ヒント")
    st.markdown("""
    - ファイルサイズ制限なし
    - 長時間録音も自動処理
    - 日本語に最適化
    """)

# メインコンテンツ
if not st.session_state.OPENAI_API_KEY:
    st.warning("⚠️ サイドバーでOpenAI APIキーを設定してください")
    st.info("APIキーは [OpenAI Platform](https://platform.openai.com/api-keys) で取得できます")
else:
    # ファイルアップロード
    uploaded_file = st.file_uploader(
        "音声ファイルをアップロード",
        type=["mp3", "wav", "m4a", "webm"],
        help="MP3, WAV, M4A, WebM形式に対応。大容量ファイルも自動処理します。"
    )
    
    if uploaded_file:
        # ファイル情報表示
        file_size_mb = uploaded_file.size / (1024 * 1024)
        st.info(f"📁 ファイル: {uploaded_file.name} ({file_size_mb:.2f} MB)")
        
        if st.button("🚀 文字起こし＆レポート生成", type="primary"):
            with st.spinner("処理中..."):
                # 一時ファイルに保存
                with tempfile.NamedTemporaryFile(delete=False, suffix=Path(uploaded_file.name).suffix) as tmp_file:
                    tmp_file.write(uploaded_file.read())
                    tmp_path = tmp_file.name
                
                try:
                    # 文字起こし
                    st.info("🎧 音声を文字起こし中...")
                    transcript = transcribe_audio(tmp_path, st.session_state.OPENAI_API_KEY)
                    
                    if transcript:
                        st.success("✓ 文字起こし完了")
                        
                        # 文字起こしテキスト表示
                        with st.expander("📄 文字起こしテキスト（クリックで表示）"):
                            st.text_area("", transcript, height=300)
                        
                        # レポート生成
                        st.info("📊 詳細レポートを生成中...")
                        report = generate_plaud_style_report(transcript, st.session_state.OPENAI_API_KEY)
                        
                        if report:
                            st.success("✓ レポート生成完了")
                            
                            # レポート表示
                            st.markdown("---")
                            st.markdown("## 📋 詳細レポート")
                            st.markdown(report)
                            
                            # ダウンロードボタン
                            col1, col2 = st.columns(2)
                            
                            with col1:
                                st.download_button(
                                    "📥 文字起こしをダウンロード",
                                    transcript,
                                    file_name=f"transcript_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                                    mime="text/plain"
                                )
                            
                            with col2:
                                st.download_button(
                                    "📥 レポートをダウンロード",
                                    report,
                                    file_name=f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                                    mime="text/markdown"
                                )
                    
                finally:
                    # 一時ファイル削除
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

# フッター
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; font-size: 0.9em;'>
    <p>🎙️ 音声メモアプリ（高品質版）| Powered by OpenAI Whisper & GPT-4o</p>
</div>
""", unsafe_allow_html=True)
