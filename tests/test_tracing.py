"""core/tracing のテスト."""

from unittest.mock import MagicMock, patch

import pytest

from ai_agent_monitoring.core.config import Settings


class TestCreateLangfuseHandler:
    """create_langfuse_handler のテスト."""

    def _get_settings(self, **overrides):
        defaults = {
            "llm_endpoint": "http://localhost:8000",
            "llm_model": "test",
            "mcp_prometheus_url": "http://localhost:9090",
            "mcp_loki_url": "http://localhost:3100",
            "mcp_grafana_url": "http://localhost:3000",
            "langfuse_enabled": True,
            "langfuse_public_key": "pk-test",
            "langfuse_secret_key": "sk-test",
            "langfuse_base_url": "http://localhost:3001",
        }
        defaults.update(overrides)
        return Settings(**defaults)

    def test_disabled(self):
        from ai_agent_monitoring.core.tracing import create_langfuse_handler

        settings = self._get_settings(langfuse_enabled=False)
        result = create_langfuse_handler(settings)
        assert result is None

    def test_no_keys(self):
        from ai_agent_monitoring.core.tracing import create_langfuse_handler

        settings = self._get_settings(langfuse_public_key="", langfuse_secret_key="")
        result = create_langfuse_handler(settings)
        assert result is None

    def test_success(self):
        from ai_agent_monitoring.core.tracing import LANGFUSE_AVAILABLE

        if not LANGFUSE_AVAILABLE:
            pytest.skip("langfuse not installed")

        from ai_agent_monitoring.core.tracing import create_langfuse_handler

        settings = self._get_settings()
        # LangfuseCallbackHandlerの初期化をモック
        with patch("ai_agent_monitoring.core.tracing.LangfuseCallbackHandler") as mock_cls:
            mock_cls.return_value = MagicMock()
            handler = create_langfuse_handler(
                settings,
                session_id="sess-1",
                tags=["alert"],
            )
        assert handler is not None
        mock_cls.assert_called_once()

    def test_not_available(self):
        """LANGFUSE_AVAILABLE=Falseの場合."""
        from ai_agent_monitoring.core import tracing

        original = tracing.LANGFUSE_AVAILABLE
        try:
            tracing.LANGFUSE_AVAILABLE = False
            settings = self._get_settings()
            result = tracing.create_langfuse_handler(settings)
            assert result is None
        finally:
            tracing.LANGFUSE_AVAILABLE = original


class TestBuildRunnableConfig:
    def _get_settings(self, **overrides):
        defaults = {
            "llm_endpoint": "http://localhost:8000",
            "llm_model": "test",
            "mcp_prometheus_url": "http://localhost:9090",
            "mcp_loki_url": "http://localhost:3100",
            "mcp_grafana_url": "http://localhost:3000",
            "langfuse_enabled": False,
        }
        defaults.update(overrides)
        return Settings(**defaults)

    def test_no_handler(self):
        from ai_agent_monitoring.core.tracing import build_runnable_config

        settings = self._get_settings(langfuse_enabled=False)
        config = build_runnable_config(settings, investigation_id="inv-1", trigger_type="alert")
        # Langfuseが無効でもrun_idは設定される（同じ調査のトレースを統合するため）
        assert "callbacks" not in config
        assert "run_id" in config

    def test_with_handler(self):
        from ai_agent_monitoring.core.tracing import build_runnable_config

        settings = self._get_settings()
        mock_handler = MagicMock()
        with patch("ai_agent_monitoring.core.tracing.create_langfuse_handler", return_value=mock_handler):
            config = build_runnable_config(settings, investigation_id="inv-1", trigger_type="alert")

        assert "callbacks" in config
        assert config["callbacks"] == [mock_handler]
        # run_idが設定されていることを確認（同じ調査のトレースを統合するため）
        assert "run_id" in config

    def test_extra_tags(self):
        from ai_agent_monitoring.core.tracing import build_runnable_config

        settings = self._get_settings()
        with patch("ai_agent_monitoring.core.tracing.create_langfuse_handler") as mock_create:
            mock_create.return_value = None
            build_runnable_config(
                settings,
                trigger_type="alert",
                extra_tags=["high-priority"],
            )
            call_kwargs = mock_create.call_args[1]
            assert "high-priority" in call_kwargs["tags"]
            assert "alert" in call_kwargs["tags"]
