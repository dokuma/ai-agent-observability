"""QueryStore の単体テスト."""

from pathlib import Path

import pytest

from ai_agent_monitoring.core.models import QueryRecord
from ai_agent_monitoring.core.query_store import QueryStore


@pytest.fixture
def store(tmp_path: Path) -> QueryStore:
    """一時ディレクトリで QueryStore を初期化."""
    db_path = str(tmp_path / "test_queries.db")
    qs = QueryStore(db_path=db_path)
    qs.initialize()
    return qs


class TestQueryStoreSaveAndGet:
    def test_save_query_returns_id(self, store: QueryStore) -> None:
        record = QueryRecord(
            query_type="promql",
            tool_name="grafana_query_prometheus",
            query_text='rate(node_cpu_seconds_total{mode="idle"}[5m])',
        )
        query_id = store.save_query("inv_001", record)
        assert isinstance(query_id, str)
        assert len(query_id) == 12

    def test_get_by_investigation(self, store: QueryStore) -> None:
        r1 = QueryRecord(
            query_type="promql",
            tool_name="grafana_query_prometheus",
            query_text="up",
        )
        r2 = QueryRecord(
            query_type="logql",
            tool_name="grafana_query_loki",
            query_text='{job="app"} |= "error"',
        )
        store.save_query("inv_001", r1)
        store.save_query("inv_001", r2)
        store.save_query(
            "inv_002",
            QueryRecord(
                query_type="k8s",
                tool_name="k8s_list_pods",
                query_text="list pods in default namespace",
            ),
        )

        results = store.get_by_investigation("inv_001")
        assert len(results) == 2
        assert results[0]["query_type"] == "promql"
        assert results[1]["query_type"] == "logql"

    def test_get_by_investigation_empty(self, store: QueryStore) -> None:
        results = store.get_by_investigation("nonexistent")
        assert results == []


class TestQueryStoreBatchSave:
    def test_save_queries_batch(self, store: QueryStore) -> None:
        records = [
            QueryRecord(
                query_type="promql",
                tool_name="grafana_query_prometheus",
                query_text=f"metric_{i}",
            )
            for i in range(5)
        ]
        count = store.save_queries("inv_batch", records)
        assert count == 5
        assert store.count() == 5

    def test_save_queries_empty(self, store: QueryStore) -> None:
        count = store.save_queries("inv_empty", [])
        assert count == 0


class TestQueryStoreSearch:
    def test_search_finds_relevant(self, store: QueryStore) -> None:
        store.save_query(
            "inv_001",
            QueryRecord(
                query_type="promql",
                tool_name="grafana_query_prometheus",
                query_text='rate(node_cpu_seconds_total{mode="idle"}[5m])',
            ),
        )
        store.save_query(
            "inv_002",
            QueryRecord(
                query_type="logql",
                tool_name="grafana_query_loki",
                query_text='{job="app"} |= "error"',
            ),
        )

        # BM25 はトークン完全一致で検索するため、ツール名やクエリタイプでマッチさせる
        results = store.search("promql grafana_query_prometheus")
        assert len(results) >= 1
        assert any("promql" == r["query_type"] for r in results)

    def test_search_no_results(self, store: QueryStore) -> None:
        results = store.search("nonexistent_metric_xyz")
        assert results == []


class TestQueryStoreCount:
    def test_count_empty(self, store: QueryStore) -> None:
        assert store.count() == 0

    def test_count_after_inserts(self, store: QueryStore) -> None:
        for i in range(3):
            store.save_query(
                "inv_001",
                QueryRecord(
                    query_type="promql",
                    tool_name="tool",
                    query_text=f"query_{i}",
                ),
            )
        assert store.count() == 3


class TestQueryStoreStatusAndError:
    def test_failed_query_saved(self, store: QueryStore) -> None:
        record = QueryRecord(
            query_type="promql",
            tool_name="grafana_query_prometheus",
            query_text="nonexistent_metric",
            status="failed",
            error_message="metric not found",
        )
        store.save_query("inv_fail", record)
        results = store.get_by_investigation("inv_fail")
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["error_message"] == "metric not found"

    def test_parameters_stored_as_json(self, store: QueryStore) -> None:
        record = QueryRecord(
            query_type="promql",
            tool_name="grafana_query_prometheus",
            query_text="up",
            parameters={"start": "2024-01-01T00:00:00Z", "end": "2024-01-02T00:00:00Z"},
        )
        store.save_query("inv_params", record)
        results = store.get_by_investigation("inv_params")
        assert len(results) == 1
        assert "2024-01-01" in results[0]["parameters_json"]


class TestQueryStoreInitReload:
    def test_reload_index_on_initialize(self, tmp_path: Path) -> None:
        """初期化時に既存レコードがBM25インデックスに読み込まれることを検証."""
        db_path = str(tmp_path / "reload.db")

        # 1回目: データを保存
        store1 = QueryStore(db_path=db_path)
        store1.initialize()
        store1.save_query(
            "inv_001",
            QueryRecord(
                query_type="promql",
                tool_name="grafana_query_prometheus",
                query_text="node_cpu_seconds_total",
            ),
        )

        # 2回目: 新しいインスタンスで検索可能か確認
        store2 = QueryStore(db_path=db_path)
        store2.initialize()
        results = store2.search("promql grafana_query_prometheus")
        assert len(results) >= 1
