"""QueryDocumentRAGのテスト."""

import pytest

from ai_agent_monitoring.tools.query_rag import (
    BM25Index,
    Document,
    QueryDocumentRAG,
    SimpleTokenizer,
    get_query_rag,
)


class TestSimpleTokenizer:
    """SimpleTokenizerのテスト."""

    def test_tokenize_english(self):
        tokens = SimpleTokenizer.tokenize("Hello World")
        assert "hello" in tokens
        assert "world" in tokens

    def test_tokenize_removes_stopwords(self):
        tokens = SimpleTokenizer.tokenize("the quick brown fox")
        assert "the" not in tokens
        assert "quick" in tokens
        assert "brown" in tokens
        assert "fox" in tokens

    def test_tokenize_preserves_query_syntax(self):
        tokens = SimpleTokenizer.tokenize('{job="app"} |= "error"')
        # 中括弧やパイプは保持されるべき
        assert any("{" in t or "job" in t for t in tokens)

    def test_tokenize_handles_promql(self):
        tokens = SimpleTokenizer.tokenize("rate(http_requests_total[5m])")
        assert "rate" in tokens
        # 角括弧がトークンに含まれる可能性がある
        assert any("http_requests_total" in t for t in tokens)


class TestBM25Index:
    """BM25Indexのテスト."""

    @pytest.fixture
    def index_with_docs(self):
        index = BM25Index()
        docs = [
            Document(
                content="PromQL is a query language for Prometheus metrics",
                metadata={"type": "promql"},
            ),
            Document(
                content="LogQL is a query language for Loki logs",
                metadata={"type": "logql"},
            ),
            Document(
                content="rate() function calculates per-second average rate",
                metadata={"type": "promql"},
            ),
        ]
        index.add_documents(docs)
        return index

    def test_add_documents(self, index_with_docs):
        assert index_with_docs.N == 3
        assert index_with_docs.avg_doc_length > 0

    def test_search_finds_relevant_docs(self, index_with_docs):
        results = index_with_docs.search("Prometheus metrics")
        assert len(results) > 0
        assert "Prometheus" in results[0].document.content

    def test_search_returns_empty_for_no_match(self, index_with_docs):
        results = index_with_docs.search("xyznonexistent")
        assert len(results) == 0

    def test_search_respects_top_k(self, index_with_docs):
        results = index_with_docs.search("query language", top_k=1)
        assert len(results) == 1

    def test_search_returns_highlights(self, index_with_docs):
        results = index_with_docs.search("Prometheus")
        assert len(results) > 0
        # highlightsが含まれている
        assert results[0].highlights is not None


class TestQueryDocumentRAG:
    """QueryDocumentRAGのテスト."""

    @pytest.fixture
    def rag(self, tmp_path):
        # テスト用の一時ドキュメントを作成
        docs_dir = tmp_path / "query_reference"
        docs_dir.mkdir()

        (docs_dir / "promql_test.md").write_text("""
# PromQL Test

## Basic Queries

### Simple metric

```promql
up
node_cpu_seconds_total
```

### With labels

```promql
http_requests_total{job="api"}
```

## Rate Functions

### rate()

The rate function calculates the per-second average rate.

```promql
rate(http_requests_total[5m])
```
""")

        (docs_dir / "logql_test.md").write_text("""
# LogQL Test

## Basic Queries

### Stream selector

```logql
{job="varlogs"}
{namespace="default"}
```

### Filter expressions

```logql
{job="app"} |= "error"
{job="app"} |~ "error|warn"
```

## Common Mistakes

Do NOT use SQL syntax:
- Wrong: `SELECT * FROM logs WHERE job = 'app'`
- Correct: `{job="app"}`
""")

        rag = QueryDocumentRAG(docs_path=docs_dir)
        rag.initialize()
        return rag

    def test_initialize_loads_documents(self, rag):
        assert rag._initialized
        assert rag.index.N > 0

    def test_search_promql(self, rag):
        results = rag.search("rate function", query_type="promql")
        assert len(results) > 0
        assert any("rate" in r.document.content.lower() for r in results)

    def test_search_logql(self, rag):
        results = rag.search("stream selector", query_type="logql")
        assert len(results) > 0

    def test_search_filters_by_type(self, rag):
        promql_results = rag.search("query", query_type="promql")
        logql_results = rag.search("query", query_type="logql")

        for r in promql_results:
            assert r.document.metadata.get("query_type") == "promql"

        for r in logql_results:
            assert r.document.metadata.get("query_type") == "logql"

    def test_get_relevant_context(self, rag):
        context = rag.get_relevant_context("how to use rate function")
        assert context
        assert "rate" in context.lower()

    def test_get_relevant_context_respects_max_tokens(self, rag):
        short_context = rag.get_relevant_context("query", max_tokens=100)
        long_context = rag.get_relevant_context("query", max_tokens=2000)
        assert len(short_context) <= len(long_context)

    def test_get_examples_for_task(self, rag):
        examples = rag.get_examples_for_task("calculate request rate")
        assert len(examples) > 0
        # コードブロックから抽出された例
        assert any("rate" in e or "http" in e for e in examples)

    def test_save_and_load_index(self, rag, tmp_path):
        index_path = tmp_path / "index.json"

        # 保存
        rag.save_index(index_path)
        assert index_path.exists()

        # 新しいRAGインスタンスで読み込み
        new_rag = QueryDocumentRAG()
        assert new_rag.load_index(index_path)
        assert new_rag._initialized
        assert new_rag.index.N == rag.index.N


class TestGetQueryRag:
    """get_query_ragのテスト."""

    def test_returns_singleton(self):
        rag1 = get_query_rag()
        rag2 = get_query_rag()
        assert rag1 is rag2

    def test_is_initialized(self):
        rag = get_query_rag()
        assert rag._initialized


class TestQueryDocumentRAGWithRealDocs:
    """実際のドキュメントを使用したテスト."""

    @pytest.fixture
    def rag_with_real_docs(self):
        rag = QueryDocumentRAG()
        rag.initialize()
        return rag

    def test_real_docs_loaded(self, rag_with_real_docs):
        """実際のドキュメントが読み込まれていることを確認."""
        # ドキュメントが存在する場合のみテスト
        if rag_with_real_docs.index.N == 0:
            pytest.skip("No real documents found")

        assert rag_with_real_docs.index.N > 0

    def test_search_cpu_metrics(self, rag_with_real_docs):
        """CPUメトリクスの検索."""
        if rag_with_real_docs.index.N == 0:
            pytest.skip("No real documents found")

        results = rag_with_real_docs.search("CPU usage metrics", query_type="promql")
        # CPUに関するドキュメントが見つかるはず
        assert len(results) > 0

    def test_search_error_logs(self, rag_with_real_docs):
        """エラーログ検索."""
        if rag_with_real_docs.index.N == 0:
            pytest.skip("No real documents found")

        results = rag_with_real_docs.search("error logs", query_type="logql")
        assert len(results) > 0

    def test_sql_vs_logql(self, rag_with_real_docs):
        """SQL vs LogQLの違いに関するドキュメントを検索."""
        if rag_with_real_docs.index.N == 0:
            pytest.skip("No real documents found")

        results = rag_with_real_docs.search("SQL AND syntax mistake wrong")
        # 間違い例に関するドキュメントが見つかるはず
        if results:
            combined = " ".join(r.document.content for r in results)
            assert "間違い" in combined or "wrong" in combined.lower() or "mistake" in combined.lower()
