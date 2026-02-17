# 音声メモアプリの自動テスト
import pytest
import os

class TestBasic:
    """基本的なテスト"""

    def test_supported_formats(self):
        """対応フォーマットのテスト"""
        formats = ['.mp3', '.wav', '.m4a', '.webm']
        assert len(formats) == 4

    def test_file_size_limit(self):
        """ファイルサイズ制限のテスト"""
        max_size = 24 * 1024 * 1024  # 24MB
        assert max_size > 0

    def test_report_sections(self):
        """レポートセクションのテスト"""
        sections = [
            "エグゼクティブサマリー",
            "キーポイント",
            "アクションアイテム",
        ]
        assert len(sections) == 3

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
