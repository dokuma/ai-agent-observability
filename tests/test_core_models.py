"""core/models のテスト."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ai_agent_monitoring.core.models import (
    Alert,
    LogEntry,
    LogExcerpt,
    PanelSnapshot,
    RCAReport,
    RootCause,
    Severity,
    TriggerType,
    UserQuery,
)


class TestAlert:
    def test_create_alert(self, sample_alert: Alert):
        assert sample_alert.alert_name == "HighCPUUsage"
        assert sample_alert.severity == Severity.CRITICAL
        assert sample_alert.instance == "web-server-01"

    def test_alert_optional_ends_at(self):
        alert = Alert(
            alert_name="Test",
            severity=Severity.INFO,
            instance="test-01",
            summary="test",
            starts_at=datetime.now(timezone.utc),
        )
        assert alert.ends_at is None


class TestUserQuery:
    def test_create_user_query(self, sample_user_query: UserQuery):
        assert "昨日" in sample_user_query.raw_input
        assert sample_user_query.time_reference == "昨日の16時ごろ"

    def test_user_query_optional_fields(self):
        query = UserQuery(raw_input="テスト")
        assert query.target_instances == []
        assert query.time_range_start is None
        assert query.time_range_end is None


class TestRootCause:
    def test_confidence_range(self):
        rc = RootCause(description="test", confidence=0.9)
        assert rc.confidence == 0.9

    def test_confidence_out_of_range(self):
        with pytest.raises(ValidationError):
            RootCause(description="test", confidence=1.5)

    def test_confidence_negative(self):
        with pytest.raises(ValidationError):
            RootCause(description="test", confidence=-0.1)


class TestRCAReport:
    def test_create_with_alert(self, sample_alert: Alert):
        report = RCAReport(
            trigger_type=TriggerType.ALERT,
            alert=sample_alert,
            root_causes=[RootCause(description="CPU spike", confidence=0.85)],
        )
        assert report.trigger_type == TriggerType.ALERT
        assert report.alert is not None
        assert report.user_query is None
        assert len(report.root_causes) == 1

    def test_create_with_user_query(self, sample_user_query: UserQuery):
        report = RCAReport(
            trigger_type=TriggerType.USER_QUERY,
            user_query=sample_user_query,
        )
        assert report.trigger_type == TriggerType.USER_QUERY
        assert report.alert is None
        assert report.user_query is not None

    def test_panel_snapshots_default_empty(self):
        report = RCAReport(trigger_type=TriggerType.ALERT)
        assert report.panel_snapshots == []
        assert report.log_excerpts == []
        assert report.markdown == ""


class TestPanelSnapshot:
    def test_create(self):
        snap = PanelSnapshot(
            dashboard_uid="abc123",
            panel_id=1,
            query="rate(cpu[5m])",
            image_path="/tmp/panel.png",
        )
        assert snap.dashboard_uid == "abc123"
        assert snap.panel_id == 1


class TestLogExcerpt:
    def test_create_with_entries(self):
        excerpt = LogExcerpt(
            query='{job="app"}',
            entries=[
                LogEntry(
                    timestamp=datetime.now(timezone.utc),
                    level="error",
                    message="test error",
                )
            ],
            caption="テスト抜粋",
        )
        assert len(excerpt.entries) == 1
        assert excerpt.caption == "テスト抜粋"
