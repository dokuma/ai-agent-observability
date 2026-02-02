"""アプリケーション設定."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """環境変数から読み込む設定."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # LLM
    llm_endpoint: str = "http://localhost:8000"
    llm_model: str = "llama-3.1-8b"

    # Monitoring Stack
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    grafana_url: str = "http://localhost:3000"
    grafana_api_key: str = ""

    # MCP Servers
    mcp_grafana_url: str = "http://localhost:8080"
    mcp_loki_url: str = "http://localhost:8081"
    mcp_prometheus_url: str = "http://localhost:8082"

    # Notifications
    slack_webhook_url: str = ""

    # Agent
    max_iterations: int = 5
    investigation_timeout_seconds: int = 120

    # Langfuse
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
