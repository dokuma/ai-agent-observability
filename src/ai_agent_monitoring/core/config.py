"""アプリケーション設定."""

import os

from pydantic import model_validator
from pydantic_settings import BaseSettings

_LLM_HEADER_PREFIX = "LLM_CUSTOM_HEADER_"


class Settings(BaseSettings):
    """環境変数から読み込む設定."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # LLM
    llm_endpoint: str = "http://localhost:8000"
    llm_model: str = "llama-3.1-8b"
    llm_api_key: str = "not-needed"
    llm_custom_headers: dict[str, str] = {}

    @model_validator(mode="after")
    def _parse_llm_custom_header_env(self) -> "Settings":
        """LLM_CUSTOM_HEADER_<KEY> 環境変数をパースしてヘッダー辞書に追加."""
        for key, value in os.environ.items():
            if key.startswith(_LLM_HEADER_PREFIX):
                header_name = key[len(_LLM_HEADER_PREFIX) :].replace("_", "-")
                self.llm_custom_headers[header_name] = value
        return self

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

    # MCP TLS
    mcp_use_tls: bool = False
    mcp_verify_ssl: bool = True
    mcp_ca_bundle: str = ""  # カスタムCA証明書パス（空の場合はシステムデフォルト）

    # CORS
    cors_allowed_origins: list[str] = ["http://localhost:3000"]

    # Langfuse
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
