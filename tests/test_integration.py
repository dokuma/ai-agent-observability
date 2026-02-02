"""結合テスト — Docker Compose環境で実行."""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# インフラ疎通テスト
# ---------------------------------------------------------------------------
class TestInfraHealth:
    """各インフラサービスへの疎通確認."""

    def test_prometheus_healthy(self) -> None:
        r = httpx.get(f"{_env('PROMETHEUS_URL', 'http://localhost:9090')}/-/healthy")
        assert r.status_code == 200

    def test_loki_ready(self) -> None:
        r = httpx.get(f"{_env('LOKI_URL', 'http://localhost:3100')}/ready")
        assert r.status_code == 200

    def test_grafana_health(self) -> None:
        r = httpx.get(f"{_env('GRAFANA_URL', 'http://localhost:3000')}/api/health")
        assert r.status_code == 200

    def test_ollama_tags(self) -> None:
        r = httpx.get(f"{_env('LLM_ENDPOINT', 'http://localhost:11434/v1').replace('/v1', '')}/api/tags")
        assert r.status_code == 200
        data = r.json()
        model_names = [m["name"] for m in data.get("models", [])]
        assert any("qwen" in n for n in model_names), f"qwen model not found: {model_names}"

    def test_langfuse_health(self) -> None:
        r = httpx.get(f"{_env('LANGFUSE_BASE_URL', 'http://localhost:3001')}/api/public/health")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# MCP サーバー疎通テスト
# ---------------------------------------------------------------------------
class TestMCPServers:
    """MCP サーバーが応答することを確認."""

    def test_prometheus_mcp_reachable(self) -> None:
        """Prometheus MCP が HTTP 接続を受け付けること."""
        url = _env("MCP_PROMETHEUS_URL", "http://localhost:9091")
        r = httpx.get(url, follow_redirects=True)
        # MCP サーバーは / で 404 を返すが接続可能であればOK
        assert r.status_code in (200, 404, 405)

    def test_loki_mcp_reachable(self) -> None:
        url = _env("MCP_LOKI_URL", "http://localhost:9092")
        r = httpx.get(url, follow_redirects=True)
        assert r.status_code in (200, 404, 405)

    def test_grafana_mcp_reachable(self) -> None:
        url = _env("MCP_GRAFANA_URL", "http://localhost:9093")
        r = httpx.get(url, follow_redirects=True)
        assert r.status_code in (200, 404, 405)


# ---------------------------------------------------------------------------
# Prometheus クエリテスト
# ---------------------------------------------------------------------------
class TestPrometheusQuery:
    """Prometheus に対してクエリが実行できること."""

    def test_query_up(self) -> None:
        url = _env("PROMETHEUS_URL", "http://localhost:9090")
        r = httpx.get(f"{url}/api/v1/query", params={"query": "up"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert len(data["data"]["result"]) > 0


# ---------------------------------------------------------------------------
# Loki クエリテスト
# ---------------------------------------------------------------------------
class TestLokiQuery:
    """Loki に対してクエリが実行できること."""

    def test_query_labels(self) -> None:
        url = _env("LOKI_URL", "http://localhost:3100")
        r = httpx.get(f"{url}/loki/api/v1/labels")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Grafana ダッシュボードテスト
# ---------------------------------------------------------------------------
class TestGrafanaDashboard:
    """Grafana のプロビジョニング済みダッシュボードを確認."""

    def test_system_overview_dashboard_exists(self) -> None:
        url = _env("GRAFANA_URL", "http://localhost:3000")
        r = httpx.get(
            f"{url}/api/dashboards/uid/system-overview",
            auth=("admin", "admin"),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["dashboard"]["title"] == "System Overview"


# ---------------------------------------------------------------------------
# LLM 推論テスト
# ---------------------------------------------------------------------------
class TestLLMInference:
    """Ollama 経由で LLM 推論が動作すること."""

    def test_chat_completion(self) -> None:
        endpoint = _env("LLM_ENDPOINT", "http://localhost:11434/v1")
        model = _env("LLM_MODEL", "qwen2.5:0.5b")
        r = httpx.post(
            f"{endpoint}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Say hello"}],
                "max_tokens": 32,
            },
            timeout=60,
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["choices"]) > 0
        assert len(data["choices"][0]["message"]["content"]) > 0


# ---------------------------------------------------------------------------
# Langfuse トレーシングテスト
# ---------------------------------------------------------------------------
class TestLangfuseTracing:
    """Langfuse にトレースが記録されることを確認."""

    def test_langchain_trace_recorded(self) -> None:
        """LangChain 経由で LLM を呼び出し、Langfuse にトレースが記録されること."""
        import os as _os

        from langchain_openai import ChatOpenAI
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

        # Langfuse v3 は環境変数で設定
        _os.environ["LANGFUSE_PUBLIC_KEY"] = _env("LANGFUSE_PUBLIC_KEY", "pk-lf-dev")
        _os.environ["LANGFUSE_SECRET_KEY"] = _env("LANGFUSE_SECRET_KEY", "sk-lf-dev")
        _os.environ["LANGFUSE_HOST"] = _env("LANGFUSE_BASE_URL", "http://localhost:3001")

        langfuse_handler = LangfuseCallbackHandler(
            trace_context={"trace_id": "integration-test-trace"},
        )

        llm = ChatOpenAI(
            model=_env("LLM_MODEL", "qwen2.5:0.5b"),
            base_url=_env("LLM_ENDPOINT", "http://localhost:11434/v1"),
            api_key="not-needed",
            max_tokens=32,
        )

        response = llm.invoke(
            "What is 1+1? Answer with just the number.",
            config={"callbacks": [langfuse_handler]},
        )
        assert len(response.content) > 0

        # Langfuse にフラッシュ
        langfuse = Langfuse()
        langfuse.flush()
        time.sleep(5)

        # Langfuse API でトレースを確認
        base_url = _env("LANGFUSE_BASE_URL", "http://localhost:3001")
        r = httpx.get(
            f"{base_url}/api/public/traces",
            auth=(
                _env("LANGFUSE_PUBLIC_KEY", "pk-lf-dev"),
                _env("LANGFUSE_SECRET_KEY", "sk-lf-dev"),
            ),
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["data"]) > 0, "Langfuse にトレースが記録されていません"

    def test_build_runnable_config_creates_handler(self) -> None:
        """build_runnable_config が Langfuse コールバックを含む config を返すこと."""
        import os as _os

        from ai_agent_monitoring.core.config import Settings
        from ai_agent_monitoring.core.tracing import build_runnable_config

        # Langfuse v3 は環境変数で設定
        _os.environ["LANGFUSE_PUBLIC_KEY"] = _env("LANGFUSE_PUBLIC_KEY", "pk-lf-dev")
        _os.environ["LANGFUSE_SECRET_KEY"] = _env("LANGFUSE_SECRET_KEY", "sk-lf-dev")
        _os.environ["LANGFUSE_HOST"] = _env("LANGFUSE_BASE_URL", "http://localhost:3001")

        settings = Settings(
            llm_endpoint=_env("LLM_ENDPOINT", "http://localhost:11434/v1"),
            llm_model=_env("LLM_MODEL", "qwen2.5:0.5b"),
            langfuse_enabled=True,
            langfuse_public_key=_env("LANGFUSE_PUBLIC_KEY", "pk-lf-dev"),
            langfuse_secret_key=_env("LANGFUSE_SECRET_KEY", "sk-lf-dev"),
            langfuse_base_url=_env("LANGFUSE_BASE_URL", "http://localhost:3001"),
        )

        config = build_runnable_config(
            settings=settings,
            investigation_id="test-inv-001",
            trigger_type="alert",
        )

        assert "callbacks" in config
        assert len(config["callbacks"]) == 1
