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

    def test_health_all_healthy(self, client):
        """全MCPが正常な場合はhealthy."""
        mock_registry = MagicMock()
        mock_registry.health_check = AsyncMock(return_value={
            "prometheus": True,
            "loki": True,
            "grafana": True,
        })
        app_state.registry = mock_registry

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["mcp_servers"]["prometheus"] is True

    def test_health_degraded(self, client):
        """一部のMCPがunhealthyな場合はdegraded."""
        mock_registry = MagicMock()
        mock_registry.health_check = AsyncMock(return_value={
            "prometheus": True,
            "loki": False,
            "grafana": True,
        })
        app_state.registry = mock_registry

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"

    def test_health_all_unhealthy(self, client):
        """全MCPがunhealthyな場合はunhealthy."""
        mock_registry = MagicMock()
        mock_registry.health_check = AsyncMock(return_value={
            "prometheus": False,
            "loki": False,
            "grafana": False,
        })
        app_state.registry = mock_registry

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


class TestInvestigationStageUpdate:
    """調査ステージ更新のテスト."""

    def test_update_stage(self, client):
        """ステージが正しく更新される."""
        inv_id = app_state.create_investigation("user_query")

        # 初期状態
        record = app_state.get_investigation(inv_id)
        assert record.current_stage == ""

        # ステージ更新
        app_state.update_investigation_stage(inv_id, "環境情報を収集中")
        record = app_state.get_investigation(inv_id)
        assert record.current_stage == "環境情報を収集中"

        # ステージ更新（iteration_countも更新）
        app_state.update_investigation_stage(inv_id, "調査計画を策定中", iteration_count=2)
        record = app_state.get_investigation(inv_id)
        assert record.current_stage == "調査計画を策定中"
        assert record.iteration_count == 2

    def test_update_stage_nonexistent(self, client):
        """存在しない調査IDでは何もしない."""
        # 例外が発生しないことを確認
        app_state.update_investigation_stage("nonexistent-id", "テスト")

    def test_status_includes_current_stage(self, client):
        """APIレスポンスにcurrent_stageが含まれる."""
        inv_id = app_state.create_investigation("user_query")
        app_state.update_investigation_stage(inv_id, "メトリクスを調査中", iteration_count=1)

        response = client.get(f"/api/v1/investigations/{inv_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["current_stage"] == "メトリクスを調査中"
        assert data["iteration_count"] == 1


class TestInvestigationTimeout:
    """調査タイムアウトのテスト."""

    @pytest.mark.asyncio
    async def test_investigation_timeout(self):
        """調査がタイムアウトした場合、failedステータスになる."""
        import asyncio

        from ai_agent_monitoring.api.routes import _run_user_query_investigation
        from ai_agent_monitoring.core.models import UserQuery

        # タイムアウトを短く設定
        app_state.settings.investigation_timeout_seconds = 1

        # 遅延するモックオーケストレータ
        mock_orchestrator = MagicMock()
        compiled = MagicMock()

        async def slow_invoke(*args, **kwargs):
            await asyncio.sleep(5)  # 5秒待機（タイムアウトより長い）
            return {"rca_report": None}

        compiled.ainvoke = slow_invoke
        mock_orchestrator.compile.return_value = compiled
        app_state.orchestrator = mock_orchestrator

        # 調査を作成
        inv_id = app_state.create_investigation("user_query")
        user_query = UserQuery(raw_input="test query")

        # タイムアウトが発生することを確認
        await _run_user_query_investigation(inv_id, user_query)

        # ステータスがfailedになっている
        record = app_state.get_investigation(inv_id)
        assert record.status == "failed"
        assert "タイムアウト" in record.error
