"""API エンドポイントのテスト."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from ai_agent_monitoring.api.dependencies import app_state
from ai_agent_monitoring.api.main import app
from ai_agent_monitoring.core.models import RCAReport, RootCause, TriggerType


@pytest.fixture
def client():
    """テスト用 FastAPI クライアント."""
    return TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoint:
    def test_health_unhealthy_no_registry(self, client):
        app_state.registry = None
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unhealthy"


class TestAlertWebhook:
    def test_webhook_empty_alerts(self, client):
        response = client.post(
            "/api/v1/webhook/alertmanager",
            json={"alerts": []},
        )
        assert response.status_code == 400

    def test_webhook_valid_alert(self, client):
        app_state.orchestrator = MagicMock()
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        app_state.orchestrator.compile.return_value = compiled

        response = client.post(
            "/api/v1/webhook/alertmanager",
            json={
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "HighCPU",
                            "severity": "warning",
                            "instance": "web-01",
                        },
                        "annotations": {
                            "summary": "CPU high",
                        },
                        "startsAt": "2026-02-01T16:00:00Z",
                    }
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert "investigation_id" in data


class TestUserQuery:
    def test_query_valid(self, client):
        app_state.orchestrator = MagicMock()
        compiled = MagicMock()
        compiled.ainvoke = AsyncMock(return_value={"rca_report": None})
        app_state.orchestrator.compile.return_value = compiled

        response = client.post(
            "/api/v1/query",
            json={"query": "昨日の4時ごろ異常がなかったか確認してください"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"

    def test_query_empty(self, client):
        response = client.post(
            "/api/v1/query",
            json={"query": ""},
        )
        assert response.status_code == 422  # validation error


class TestInvestigationStatus:
    def test_not_found(self, client):
        response = client.get("/api/v1/investigations/nonexistent")
        assert response.status_code == 404

    def test_get_status(self, client):
        inv_id = app_state.create_investigation("alert")
        response = client.get(f"/api/v1/investigations/{inv_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["trigger_type"] == "alert"


class TestInvestigationReport:
    def test_not_found(self, client):
        response = client.get("/api/v1/investigations/nonexistent/report")
        assert response.status_code == 404

    def test_still_running(self, client):
        inv_id = app_state.create_investigation("alert")
        response = client.get(f"/api/v1/investigations/{inv_id}/report")
        assert response.status_code == 409

    def test_failed(self, client):
        inv_id = app_state.create_investigation("alert")
        app_state.fail_investigation(inv_id, "test error")
        response = client.get(f"/api/v1/investigations/{inv_id}/report")
        assert response.status_code == 500

    def test_completed_with_report(self, client):
        inv_id = app_state.create_investigation("alert")
        report = RCAReport(
            trigger_type=TriggerType.ALERT,
            root_causes=[RootCause(description="test cause", confidence=0.8)],
            metrics_summary="test metrics",
            logs_summary="test logs",
            recommendations=["fix it"],
            markdown="# Test Report",
        )
        app_state.complete_investigation(inv_id, rca_report=report)

        response = client.get(f"/api/v1/investigations/{inv_id}/report")
        assert response.status_code == 200
        data = response.json()
        assert data["markdown"] == "# Test Report"
        assert len(data["root_causes"]) == 1
        assert data["root_causes"][0]["confidence"] == 0.8
