"""ReportStoreのテスト."""

from pathlib import Path

import pytest

from ai_agent_monitoring.core.models import (
    Alert,
    RCAReport,
    RootCause,
    Severity,
    TriggerType,
    UserQuery,
)
from ai_agent_monitoring.core.report_store import ReportStore


def _make_alert_report(**kwargs) -> RCAReport:
    """テスト用のアラート起動RCAレポートを作成."""
    defaults = {
        "trigger_type": TriggerType.ALERT,
        "alert": Alert(
            alert_name="HighMemory",
            severity=Severity.CRITICAL,
            instance="web-01",
            summary="Memory usage exceeded 90%",
            starts_at="2026-01-15T10:00:00Z",
        ),
        "root_causes": [
            RootCause(
                description="OOMKill detected in nginx pod due to memory leak",
                confidence=0.9,
                evidence=["container_memory_usage > 90%"],
            ),
        ],
        "metrics_summary": "Memory usage reached 95% on web-01",
        "logs_summary": "OOMKill events in nginx container logs",
        "k8s_summary": "Pod nginx-abc123 restarted 3 times",
        "recommendations": ["Increase memory limit", "Fix memory leak"],
        "markdown": "# RCA Report\n## OOMKill in nginx",
    }
    defaults.update(kwargs)
    return RCAReport(**defaults)


def _make_query_report(**kwargs) -> RCAReport:
    """テスト用のユーザクエリ起動RCAレポートを作成."""
    defaults = {
        "trigger_type": TriggerType.USER_QUERY,
        "user_query": UserQuery(raw_input="なぜnginxがクラッシュしたか"),
        "root_causes": [
            RootCause(
                description="nginx configuration error causing crash loop",
                confidence=0.85,
            ),
        ],
        "metrics_summary": "Pod restart count: 5 in last hour",
        "logs_summary": "nginx: invalid config syntax at line 42",
        "recommendations": ["Fix nginx.conf syntax error"],
        "markdown": "# RCA Report\n## Nginx crash",
    }
    defaults.update(kwargs)
    return RCAReport(**defaults)


@pytest.fixture
def store(tmp_path):
    """一時ディレクトリを使用するReportStoreフィクスチャ."""
    db_path = str(tmp_path / "test_reports.db")
    s = ReportStore(db_path=db_path)
    s.initialize()
    return s


class TestReportStoreBasic:
    def test_initialize_creates_db(self, tmp_path):
        db_path = str(tmp_path / "subdir" / "test.db")
        s = ReportStore(db_path=db_path)
        s.initialize()
        assert Path(db_path).exists()

    def test_empty_store(self, store):
        assert store.count() == 0
        reports, total = store.list_reports()
        assert total == 0
        assert reports == []

    def test_save_and_get(self, store):
        report = _make_alert_report()
        report_id = store.save_report("inv-001", report)

        assert len(report_id) == 12
        assert store.count() == 1

        stored = store.get_report(report_id)
        assert stored is not None
        assert stored.id == report_id
        assert stored.investigation_id == "inv-001"
        assert stored.report.trigger_type == TriggerType.ALERT
        assert len(stored.report.root_causes) == 1

    def test_get_nonexistent(self, store):
        assert store.get_report("nonexistent") is None

    def test_list_reports_ordering(self, store):
        """レポートは新しい順に返される."""
        store.save_report("inv-001", _make_alert_report())
        store.save_report("inv-002", _make_query_report())

        reports, total = store.list_reports()
        assert total == 2
        assert len(reports) == 2
        # 新しい順
        assert reports[0].investigation_id == "inv-002"
        assert reports[1].investigation_id == "inv-001"

    def test_list_reports_pagination(self, store):
        for i in range(5):
            store.save_report(f"inv-{i:03d}", _make_alert_report())

        reports, total = store.list_reports(offset=0, limit=2)
        assert total == 5
        assert len(reports) == 2

        reports2, _ = store.list_reports(offset=2, limit=2)
        assert len(reports2) == 2

        reports3, _ = store.list_reports(offset=4, limit=2)
        assert len(reports3) == 1


class TestReportStoreSearch:
    def test_search_by_keyword(self, store):
        store.save_report("inv-001", _make_alert_report())
        store.save_report("inv-002", _make_query_report())

        results = store.search("OOMKill")
        assert len(results) >= 1
        report, score, _highlights = results[0]
        assert "inv-001" == report.investigation_id
        assert score > 0

    def test_search_by_alert_name(self, store):
        store.save_report("inv-001", _make_alert_report())
        results = store.search("HighMemory")
        assert len(results) >= 1

    def test_search_by_recommendation(self, store):
        store.save_report("inv-001", _make_alert_report())
        results = store.search("memory leak")
        assert len(results) >= 1

    def test_search_no_results(self, store):
        store.save_report("inv-001", _make_alert_report())
        results = store.search("database connection timeout")
        # BM25 may return results with low scores, but for completely unrelated queries
        # either no results or very low scores
        if results:
            _, score, _ = results[0]
            assert score < 1.0  # very low relevance

    def test_search_japanese_query(self, store):
        store.save_report("inv-001", _make_query_report())
        results = store.search("nginx クラッシュ")
        assert len(results) >= 1

    def test_search_top_k(self, store):
        for i in range(10):
            store.save_report(f"inv-{i:03d}", _make_alert_report())
        results = store.search("memory", top_k=3)
        assert len(results) <= 3


class TestReportStoreIndexRebuild:
    def test_rebuild_index_on_init(self, tmp_path):
        """再起動時にBM25インデックスが既存DBから再構築される."""
        db_path = str(tmp_path / "test.db")

        # 1回目: レポートを保存
        store1 = ReportStore(db_path=db_path)
        store1.initialize()
        store1.save_report("inv-001", _make_alert_report())
        store1.save_report("inv-002", _make_query_report())

        # 2回目: 新しいインスタンスで再初期化
        store2 = ReportStore(db_path=db_path)
        store2.initialize()

        assert store2.count() == 2
        results = store2.search("OOMKill")
        assert len(results) >= 1
