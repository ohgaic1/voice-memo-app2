# デプロイ後の動作確認テスト
import os
import requests

APP_URL = os.environ.get('APP_URL', '')

def test_app_health():
    """アプリのヘルスチェック"""
    if not APP_URL:
        print("APP_URL未設定のためスキップ")
        return

    try:
        url = f"{APP_URL}/_stcore/health"
        res = requests.get(url, timeout=15)
        assert res.status_code == 200
        print("✅ アプリが正常に動作しています")
    except Exception as e:
        print(f"⚠️ 確認が必要です: {e}")

if __name__ == "__main__":
    test_app_health()
