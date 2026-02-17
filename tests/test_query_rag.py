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


class TestDetectQueryTypeEndpoints:
    """_detect_query_typeのエンドポイントタイプ検出テスト."""

    def test_detect_promql(self):
        rag = QueryDocumentRAG()
        assert rag._detect_query_type("promql_basics.md") == "promql"

    def test_detect_logql(self):
        rag = QueryDocumentRAG()
        assert rag._detect_query_type("logql_examples.md") == "logql"

    def test_detect_prometheus_endpoint(self):
        rag = QueryDocumentRAG()
        assert rag._detect_query_type("prometheus_endpoints.md") == "prometheus_endpoint"

    def test_detect_loki_endpoint(self):
        rag = QueryDocumentRAG()
        assert rag._detect_query_type("loki_endpoints.md") == "loki_endpoint"

    def test_detect_unknown(self):
        rag = QueryDocumentRAG()
        assert rag._detect_query_type("random_file.md") == "unknown"

    def test_detect_case_insensitive(self):
        rag = QueryDocumentRAG()
        assert rag._detect_query_type("Prometheus_Endpoints.md") == "prometheus_endpoint"
        assert rag._detect_query_type("LOKI_ENDPOINTS.md") == "loki_endpoint"


class TestQueryDocumentRAGWithEndpoints:
    """エンドポイントドキュメント付きRAGのテスト."""

    @pytest.fixture
    def rag_with_endpoints(self, tmp_path):
        docs_dir = tmp_path / "query_reference"
        docs_dir.mkdir()

        (docs_dir / "prometheus_endpoints.md").write_text("""
# Prometheus HTTP API エンドポイントリファレンス

## クエリAPI (Query API)

### インスタントクエリ (Instant Query)

`GET /api/v1/query`

特定時点のPromQLクエリを実行してメトリクスを取得するエンドポイントです。

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `query` | はい | PromQL式 |
| `time` | いいえ | 評価タイムスタンプ |

```bash
curl 'http://localhost:9090/api/v1/query?query=up'
```

### レンジクエリ (Range Query)

`GET /api/v1/query_range`

時間範囲を指定してPromQLクエリを実行します。グラフ描画やトレンド分析に使用します。

```bash
curl 'http://localhost:9090/api/v1/query_range?query=rate(http_requests_total[5m])&start=2024-01-01T00:00:00Z&end=2024-01-01T01:00:00Z&step=15s'
```

## メタデータAPI (Metadata API)

### ラベル一覧取得 (Get Label Names)

`GET /api/v1/labels`

Prometheusに存在する全てのラベル名の一覧を取得します。

```bash
curl 'http://localhost:9090/api/v1/labels'
```

## 管理API (Admin API)

### ヘルスチェック (Health Check)

`GET /-/healthy`

Prometheusサーバーの死活監視に使用するヘルスチェックエンドポイントです。

```bash
curl 'http://localhost:9090/-/healthy'
```

### 準備状態確認 (Readiness Check)

`GET /-/ready`

サーバーがリクエストを受け付ける準備ができているか確認します。

```bash
curl 'http://localhost:9090/-/ready'
```

## ターゲットAPI (Targets API)

### ターゲット一覧 (List Targets)

`GET /api/v1/targets`

スクレイプターゲットの一覧と状態を取得します。監視対象の確認に使用します。

```bash
curl 'http://localhost:9090/api/v1/targets'
```

## アラートAPI (Alerts API)

### アラート確認 (Get Alerts)

`GET /api/v1/alerts`

現在発火中のアラート一覧を取得します。アラート確認・障害対応に使用します。

```bash
curl 'http://localhost:9090/api/v1/alerts'
```

## ステータスAPI (Status API)

### 設定確認 (Get Config)

`GET /api/v1/status/config`

現在のPrometheus設定をYAML形式で取得します。設定確認やデバッグに使用します。

```bash
curl 'http://localhost:9090/api/v1/status/config'
```
""")

        (docs_dir / "loki_endpoints.md").write_text("""
# Loki HTTP API エンドポイントリファレンス

## クエリAPI (Query API)

### インスタントクエリ (Instant Query)

`GET /loki/api/v1/query`

特定時点のLogQLクエリを実行してログを取得するエンドポイントです。

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `query` | はい | LogQL式 |
| `time` | いいえ | 評価タイムスタンプ |
| `limit` | いいえ | 返すエントリ数の上限 |

```bash
curl 'http://localhost:3100/loki/api/v1/query?query={job="app"}'
```

### レンジクエリ (Range Query)

`GET /loki/api/v1/query_range`

時間範囲を指定してLogQLクエリを実行します。ログ検索やトレンド分析に使用します。

```bash
curl 'http://localhost:3100/loki/api/v1/query_range?query={job="app"}&start=1609459200&end=1609462800'
```

## メタデータAPI (Metadata API)

### ラベル一覧取得 (Get Label Names)

`GET /loki/api/v1/labels`

Lokiに存在する全てのラベル名の一覧を取得します。

```bash
curl 'http://localhost:3100/loki/api/v1/labels'
```

## ログ送信API (Push API)

### ログ送信 (Push Logs)

`POST /loki/api/v1/push`

ログエントリをLokiに送信するエンドポイントです。ログストリームをプッシュします。

```bash
curl -X POST 'http://localhost:3100/loki/api/v1/push' \\
  -H 'Content-Type: application/json' \\
  -d '{"streams":[{"stream":{"job":"test"},"values":[["1609459200000000000","test log"]]}]}'
```

## テールAPI (Tail API)

### リアルタイムログ取得 (Tail Logs)

`GET /loki/api/v1/tail`

WebSocket経由でログをリアルタイムにテールするエンドポイントです。

```bash
curl 'http://localhost:3100/loki/api/v1/tail?query={job="app"}'
```

## 管理API (Admin API)

### ヘルスチェック (Health Check)

`GET /ready`

Lokiサーバーの死活監視に使用するヘルスチェックエンドポイントです。

```bash
curl 'http://localhost:3100/ready'
```
""")

        rag = QueryDocumentRAG(docs_path=docs_dir)
        rag.initialize()
        return rag

    def test_endpoint_docs_loaded(self, rag_with_endpoints):
        """エンドポイントドキュメントが読み込まれていることを確認."""
        assert rag_with_endpoints._initialized
        assert rag_with_endpoints.index.N > 0

    def test_search_prometheus_endpoint(self, rag_with_endpoints):
        """Prometheusエンドポイント検索."""
        results = rag_with_endpoints.search(
            "instant query api/v1/query",
            query_type="prometheus_endpoint",
        )
        assert len(results) > 0
        assert any("/api/v1/query" in r.document.content for r in results)

    def test_search_loki_endpoint(self, rag_with_endpoints):
        """Lokiエンドポイント検索."""
        results = rag_with_endpoints.search(
            "log push api",
            query_type="loki_endpoint",
        )
        assert len(results) > 0
        assert any("push" in r.document.content.lower() for r in results)

    def test_search_filters_endpoint_type(self, rag_with_endpoints):
        """エンドポイントタイプフィルタの動作確認."""
        prom_results = rag_with_endpoints.search(
            "query", query_type="prometheus_endpoint"
        )
        loki_results = rag_with_endpoints.search(
            "query", query_type="loki_endpoint"
        )

        for r in prom_results:
            assert r.document.metadata.get("query_type") == "prometheus_endpoint"

        for r in loki_results:
            assert r.document.metadata.get("query_type") == "loki_endpoint"

    def test_search_healthcheck_keyword(self, rag_with_endpoints):
        """ヘルスチェックキーワード検索."""
        results = rag_with_endpoints.search("ヘルスチェック 死活監視")
        assert len(results) > 0
        combined = " ".join(r.document.content for r in results)
        assert "healthy" in combined.lower() or "ready" in combined.lower()

    def test_search_labels_japanese(self, rag_with_endpoints):
        """日本語キーワードでのラベル一覧検索."""
        results = rag_with_endpoints.search("ラベル一覧取得")
        assert len(results) > 0
        combined = " ".join(r.document.content for r in results)
        assert "labels" in combined.lower() or "ラベル" in combined

    def test_search_targets(self, rag_with_endpoints):
        """ターゲット一覧のキーワード検索."""
        results = rag_with_endpoints.search("ターゲット一覧 スクレイプ 監視対象")
        assert len(results) > 0
        combined = " ".join(r.document.content for r in results)
        assert "targets" in combined.lower() or "ターゲット" in combined

    def test_search_alerts(self, rag_with_endpoints):
        """アラート確認の検索."""
        results = rag_with_endpoints.search("アラート確認 障害対応")
        assert len(results) > 0
        combined = " ".join(r.document.content for r in results)
        assert "alerts" in combined.lower() or "アラート" in combined

    def test_search_loki_tail_realtime(self, rag_with_endpoints):
        """Lokiテール・リアルタイムログ検索."""
        results = rag_with_endpoints.search("リアルタイム テール tail")
        assert len(results) > 0
        combined = " ".join(r.document.content for r in results)
        assert "tail" in combined.lower()

    def test_search_config_status(self, rag_with_endpoints):
        """設定確認ステータス検索."""
        results = rag_with_endpoints.search("設定確認 config status")
        assert len(results) > 0
        combined = " ".join(r.document.content for r in results)
        assert "config" in combined.lower() or "設定" in combined

    def test_no_type_filter_includes_all(self, rag_with_endpoints):
        """タイプフィルタなしで全ドキュメントが検索対象."""
        results = rag_with_endpoints.search("query", query_type=None)
        assert len(results) > 0
        query_types = {r.document.metadata.get("query_type") for r in results}
        # PrometheusとLoki両方のエンドポイントが含まれる
        assert len(query_types) >= 2


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

        results = rag_with_real_docs.search("SQL AND syntax mistake wrong", top_k=10)
        # 間違い例に関するドキュメントが見つかるはず
        if results:
            combined = " ".join(r.document.content for r in results)
            combined_lower = combined.lower()
            assert (
                "間違い" in combined
                or "wrong" in combined_lower
                or "mistake" in combined_lower
                or "構文" in combined
                or "syntax" in combined_lower
            )
