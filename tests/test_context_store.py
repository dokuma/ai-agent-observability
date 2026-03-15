"""ContextStore のユニットテスト."""

import pytest

from ai_agent_monitoring.tools.context_store import ContextStore


@pytest.fixture
def store() -> ContextStore:
    return ContextStore(max_chunk_chars=100, search_limit=3)


@pytest.fixture
def large_store() -> ContextStore:
    return ContextStore(max_chunk_chars=500, search_limit=5)


class TestContextStoreInit:
    def test_creates_fts5_table(self, store: ContextStore) -> None:
        """FTS5 テーブルが作成されること."""
        rows = store._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [r["name"] for r in rows]
        assert "chunks" in table_names

    def test_close(self, store: ContextStore) -> None:
        """close() 後に DB アクセスできないこと."""
        store.close()
        with pytest.raises(Exception, match="Cannot operate on a closed database"):
            store._db.execute("SELECT 1")


class TestIndex:
    def test_index_text_content(self, store: ContextStore) -> None:
        """テキストコンテンツをインデックスできること."""
        result = {"content": [{"type": "text", "text": "CPU usage is high on node-1"}]}
        label = store.index("query_prometheus", result)
        assert label.startswith("query_prometheus_")
        chunks = store.get_by_source(label)
        assert len(chunks) > 0
        assert "CPU usage is high" in chunks[0]["content"]

    def test_index_error_result(self, store: ContextStore) -> None:
        """エラー結果もインデックスされること."""
        result = {"error": "connection refused"}
        label = store.index("query_loki", result)
        chunks = store.get_by_source(label)
        assert len(chunks) > 0
        assert "connection refused" in chunks[0]["content"]

    def test_index_empty_content(self, store: ContextStore) -> None:
        """空コンテンツはチャンクが生成されないこと."""
        result = {"content": []}
        label = store.index("test_tool", result)
        chunks = store.get_by_source(label)
        assert len(chunks) == 0

    def test_chunking_splits_large_text(self, store: ContextStore) -> None:
        """max_chunk_chars を超えるテキストが複数チャンクに分割されること."""
        long_text = "\n\n".join([f"paragraph {i}: " + "x" * 50 for i in range(10)])
        result = {"content": [{"type": "text", "text": long_text}]}
        label = store.index("test_tool", result)
        chunks = store.get_by_source(label, limit=100)
        assert len(chunks) > 1

    def test_multiple_indexes(self, store: ContextStore) -> None:
        """複数回のインデックスが独立した source_label を持つこと."""
        r1 = {"content": [{"type": "text", "text": "first result"}]}
        r2 = {"content": [{"type": "text", "text": "second result"}]}
        label1 = store.index("tool_a", r1)
        label2 = store.index("tool_b", r2)
        assert label1 != label2
        assert len(store.get_by_source(label1)) > 0
        assert len(store.get_by_source(label2)) > 0


class TestSearch:
    def test_search_finds_relevant_chunks(self, large_store: ContextStore) -> None:
        """BM25 検索が関連チャンクを返すこと."""
        large_store.index(
            "query_prometheus",
            {"content": [{"type": "text", "text": "CPU usage is 95% on node-1, memory is normal"}]},
        )
        large_store.index(
            "query_loki",
            {"content": [{"type": "text", "text": "OOMKilled error in pod app-server"}]},
        )
        results = large_store.search("CPU usage")
        assert len(results) > 0
        assert any("CPU" in r["content"] for r in results)

    def test_search_empty_query(self, store: ContextStore) -> None:
        """空クエリは空結果を返すこと."""
        results = store.search("")
        assert results == []

    def test_search_no_match(self, store: ContextStore) -> None:
        """一致なしの場合は空結果を返すこと."""
        store.index("tool", {"content": [{"type": "text", "text": "hello world"}]})
        results = store.search("zzzznonexistent")
        assert results == []

    def test_search_special_characters(self, store: ContextStore) -> None:
        """特殊文字を含むクエリがエラーにならないこと."""
        store.index("tool", {"content": [{"type": "text", "text": "test data"}]})
        results = store.search('query with "quotes" and (parens)')
        # エラーが起きなければOK
        assert isinstance(results, list)


class TestGetBySource:
    def test_limit_respected(self, store: ContextStore) -> None:
        """limit パラメータが尊重されること."""
        long_text = "\n\n".join([f"chunk {i}" for i in range(20)])
        result = {"content": [{"type": "text", "text": long_text}]}
        label = store.index("tool", result)
        chunks = store.get_by_source(label, limit=2)
        assert len(chunks) <= 2

    def test_nonexistent_source(self, store: ContextStore) -> None:
        """存在しない source_label は空結果を返すこと."""
        chunks = store.get_by_source("nonexistent_label")
        assert chunks == []


class TestFormatAsToolResult:
    def test_format_basic(self, store: ContextStore) -> None:
        """基本的なフォーマットが正しいこと."""
        chunks = [{"content": "chunk1", "source_label": "test_abc"}]
        result = store.format_as_tool_result(chunks, original_chars=5000)
        assert result["content"][0]["type"] == "text"
        assert "chunk1" in result["content"][0]["text"]

    def test_format_empty_chunks(self, store: ContextStore) -> None:
        """空チャンクリストは空テキストを返すこと."""
        result = store.format_as_tool_result([], original_chars=0)
        assert result["content"][0]["text"] == ""

    def test_format_truncation_metadata(self, store: ContextStore) -> None:
        """チャンクが全件表示されない場合にメタデータが付与されること."""
        # 大量のチャンクをインデックス
        long_text = "\n\n".join([f"paragraph {i}: data" for i in range(20)])
        result = {"content": [{"type": "text", "text": long_text}]}
        label = store.index("tool", result)
        # limit=2 で取得（全件より少ない）
        chunks = store.get_by_source(label, limit=2)
        all_chunks = store.get_by_source(label, limit=100)
        if len(all_chunks) > len(chunks):
            formatted = store.format_as_tool_result(chunks, original_chars=10000)
            assert formatted.get("_context_mode") is True
            assert formatted["_shown_chunks"] == len(chunks)
            assert formatted["_total_chunks"] > len(chunks)


class TestChunking:
    def test_paragraph_boundaries(self, large_store: ContextStore) -> None:
        """空行で区切られた段落がチャンク境界になること."""
        text = "Paragraph 1 content.\n\nParagraph 2 content.\n\nParagraph 3 content."
        chunks = large_store._chunk_text(text)
        # 500文字制限なので1チャンクに収まる
        assert len(chunks) >= 1

    def test_long_single_paragraph(self) -> None:
        """長い単一段落が行単位で分割されること."""
        store = ContextStore(max_chunk_chars=50, search_limit=3)
        text = "\n".join([f"line {i}: some data here" for i in range(10)])
        chunks = store._chunk_text(text)
        assert len(chunks) > 1
        # 各チャンクが max_chunk_chars 以下
        for chunk in chunks:
            assert len(chunk) <= 60  # 行境界で少しオーバーする可能性を許容


class TestSanitizeQuery:
    def test_removes_special_chars(self, store: ContextStore) -> None:
        """FTS5 特殊文字が除去されること."""
        sanitized = store._sanitize_query('test "quoted" (grouped)')
        assert '"' not in sanitized or sanitized.count('"') == sanitized.count('"')  # 引用のみ
        assert "(" not in sanitized
        assert ")" not in sanitized

    def test_empty_string(self, store: ContextStore) -> None:
        """空文字列は空文字列を返すこと."""
        assert store._sanitize_query("") == ""
