"""API dependencies / main のテスト."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_agent_monitoring.api.dependencies import AppState
from ai_agent_monitoring.core.models import RCAReport, TriggerType


class TestAppStateInitialize:
    @pytest.mark.asyncio
    async def test_initialize(self):
        app = AppState()

        mock_registry = MagicMock()
        mock_registry.health_check = AsyncMock(return_value={
            "prometheus": True, "loki": True, "grafana": True,
        })
        mock_registry.prometheus = MagicMock()
        mock_registry.prometheus.client = MagicMock()
        mock_registry.loki = MagicMock()
        mock_registry.loki.client = MagicMock()
        mock_registry.grafana = MagicMock()
        mock_registry.grafana.client = MagicMock()

        with patch("ai_agent_monitoring.api.dependencies.ToolRegistry") as mock_tr_cls, \
             patch("ai_agent_monitoring.api.dependencies.ChatOpenAI") as mock_llm_cls, \
             patch("ai_agent_monitoring.api.dependencies.OrchestratorAgent") as mock_orch_cls:
            mock_tr_cls.from_settings.return_value = mock_registry
            mock_llm_cls.return_value = MagicMock()
            mock_orch_cls.return_value = MagicMock()

            await app.initialize()

        assert app.registry is not None
        assert app.orchestrator is not None
        mock_registry.health_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown(self):
        app = AppState()
        # shutdown はログ出力のみなのでエラーなく完了すればOK
        await app.shutdown()


class TestAppStateInvestigations:
    def test_create_and_get(self):
        app = AppState()
        inv_id = app.create_investigation("alert")
        record = app.get_investigation(inv_id)

        assert record is not None
        assert record.status == "running"
        assert record.trigger_type == "alert"

    def test_complete(self):
        app = AppState()
        inv_id = app.create_investigation("user_query")
        report = RCAReport(trigger_type=TriggerType.USER_QUERY)
        app.complete_investigation(inv_id, rca_report=report)

        record = app.get_investigation(inv_id)
        assert record.status == "completed"
        assert record.rca_report is not None
        assert record.completed_at is not None

    def test_fail(self):
        app = AppState()
        inv_id = app.create_investigation("alert")
        app.fail_investigation(inv_id, "timeout")

        record = app.get_investigation(inv_id)
        assert record.status == "failed"
        assert record.error == "timeout"

    def test_get_nonexistent(self):
        app = AppState()
        assert app.get_investigation("nonexistent") is None


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan(self):
        """main.py の lifespan コンテキストマネージャのテスト."""
        from ai_agent_monitoring.api.main import app, lifespan

        with patch("ai_agent_monitoring.api.main.app_state") as mock_state:
            mock_state.initialize = AsyncMock()
            mock_state.shutdown = AsyncMock()

            async with lifespan(app):
                mock_state.initialize.assert_called_once()

            mock_state.shutdown.assert_called_once()
