"""core/config.py (Settings) のテスト."""

import os

from ai_agent_monitoring.core.config import Settings


def _clean_settings(**overrides: object) -> Settings:
    """環境変数と .env ファイルの影響を排除して Settings を生成.

    _env_file=None でファイル読み込みを無効化し、
    Settings が定義するフィールド名に対応する環境変数を除去してから構築する。
    """
    # Settings フィールド名に対応する環境変数をすべて列挙
    field_env_keys = [name.upper() for name in Settings.model_fields]
    saved: dict[str, str] = {}
    for key in field_env_keys:
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    try:
        return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
    finally:
        os.environ.update(saved)


class TestSettingsDefaults:
    """デフォルト値の検証 (.env / 環境変数なし)."""

    def test_llm_defaults(self):
        """LLM関連のデフォルト値."""
        s = _clean_settings()
        assert s.llm_endpoint == "http://localhost:8000"
        assert s.llm_model == "llama-3.1-8b"
        assert s.llm_api_key == "not-needed"

    def test_monitoring_stack_defaults(self):
        """監視スタック関連のデフォルト値."""
        s = _clean_settings()
        assert s.prometheus_url == "http://localhost:9090"
        assert s.loki_url == "http://localhost:3100"
        assert s.grafana_url == "http://localhost:3000"
        assert s.grafana_api_key == ""

    def test_mcp_server_defaults(self):
        """MCPサーバー関連のデフォルト値."""
        s = _clean_settings()
        assert s.mcp_grafana_url == "http://localhost:8080"
        assert s.mcp_loki_url == "http://localhost:8081"
        assert s.mcp_prometheus_url == "http://localhost:8082"

    def test_notification_defaults(self):
        """通知関連のデフォルト値."""
        s = _clean_settings()
        assert s.slack_webhook_url == ""

    def test_agent_defaults(self):
        """エージェント関連のデフォルト値."""
        s = _clean_settings()
        assert s.max_iterations == 5
        assert s.investigation_timeout_seconds == 120

    def test_langfuse_defaults(self):
        """Langfuse関連のデフォルト値."""
        s = _clean_settings()
        assert s.langfuse_enabled is True
        assert s.langfuse_public_key == ""
        assert s.langfuse_secret_key == ""
        assert s.langfuse_base_url == "https://cloud.langfuse.com"


class TestSettingsFromEnv:
    """環境変数からの設定読み込み."""

    def test_llm_endpoint_from_env(self, monkeypatch):
        """LLMエンドポイントを環境変数から読み込み."""
        monkeypatch.setenv("LLM_ENDPOINT", "http://custom-llm:9000")
        s = Settings()
        assert s.llm_endpoint == "http://custom-llm:9000"

    def test_llm_model_from_env(self, monkeypatch):
        """LLMモデルを環境変数から読み込み."""
        monkeypatch.setenv("LLM_MODEL", "gpt-4")
        s = Settings()
        assert s.llm_model == "gpt-4"

    def test_llm_api_key_from_env(self, monkeypatch):
        """LLM APIキーを環境変数から読み込み."""
        monkeypatch.setenv("LLM_API_KEY", "sk-secret-key")
        s = Settings()
        assert s.llm_api_key == "sk-secret-key"

    def test_prometheus_url_from_env(self, monkeypatch):
        """Prometheus URLを環境変数から読み込み."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://prom:9090")
        s = Settings()
        assert s.prometheus_url == "http://prom:9090"

    def test_mcp_urls_from_env(self, monkeypatch):
        """MCP URLsを環境変数から読み込み."""
        monkeypatch.setenv("MCP_GRAFANA_URL", "http://grafana-mcp:8080")
        monkeypatch.setenv("MCP_LOKI_URL", "http://loki-mcp:8081")
        monkeypatch.setenv("MCP_PROMETHEUS_URL", "http://prom-mcp:8082")
        s = Settings()
        assert s.mcp_grafana_url == "http://grafana-mcp:8080"
        assert s.mcp_loki_url == "http://loki-mcp:8081"
        assert s.mcp_prometheus_url == "http://prom-mcp:8082"

    def test_max_iterations_from_env(self, monkeypatch):
        """max_iterationsを環境変数から読み込み."""
        monkeypatch.setenv("MAX_ITERATIONS", "10")
        s = Settings()
        assert s.max_iterations == 10

    def test_investigation_timeout_from_env(self, monkeypatch):
        """investigation_timeout_secondsを環境変数から読み込み."""
        monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "300")
        s = Settings()
        assert s.investigation_timeout_seconds == 300

    def test_langfuse_enabled_from_env(self, monkeypatch):
        """langfuse_enabledを環境変数から読み込み."""
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        s = Settings()
        assert s.langfuse_enabled is False

    def test_multiple_env_vars(self, monkeypatch):
        """複数の環境変数を同時に読み込み."""
        monkeypatch.setenv("LLM_ENDPOINT", "http://llm:8000")
        monkeypatch.setenv("GRAFANA_URL", "http://grafana:3000")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/xxx")
        s = Settings()
        assert s.llm_endpoint == "http://llm:8000"
        assert s.grafana_url == "http://grafana:3000"
        assert s.slack_webhook_url == "https://hooks.slack.com/xxx"


class TestSettingsTypes:
    """各フィールドの型検証."""

    def test_string_fields(self):
        """文字列フィールドの型確認."""
        s = Settings()
        str_fields = [
            "llm_endpoint",
            "llm_model",
            "llm_api_key",
            "prometheus_url",
            "loki_url",
            "grafana_url",
            "grafana_api_key",
            "mcp_grafana_url",
            "mcp_loki_url",
            "mcp_prometheus_url",
            "slack_webhook_url",
            "langfuse_public_key",
            "langfuse_secret_key",
            "langfuse_base_url",
        ]
        for field in str_fields:
            assert isinstance(getattr(s, field), str), f"{field} should be str"

    def test_int_fields(self):
        """整数フィールドの型確認."""
        s = Settings()
        assert isinstance(s.max_iterations, int)
        assert isinstance(s.investigation_timeout_seconds, int)

    def test_bool_fields(self):
        """ブールフィールドの型確認."""
        s = Settings()
        assert isinstance(s.langfuse_enabled, bool)

    def test_int_coercion_from_env(self, monkeypatch):
        """環境変数からの整数変換."""
        monkeypatch.setenv("MAX_ITERATIONS", "20")
        s = Settings()
        assert s.max_iterations == 20
        assert isinstance(s.max_iterations, int)

    def test_bool_coercion_from_env(self, monkeypatch):
        """環境変数からのブール値変換."""
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        s = Settings()
        assert s.langfuse_enabled is True

        monkeypatch.setenv("LANGFUSE_ENABLED", "0")
        s2 = Settings()
        assert s2.langfuse_enabled is False


class TestSettingsExtraIgnore:
    """extra = "ignore" の挙動テスト."""

    def test_unknown_env_vars_ignored(self, monkeypatch):
        """未知の環境変数が無視されること."""
        monkeypatch.setenv("TOTALLY_UNKNOWN_SETTING", "some_value")
        monkeypatch.setenv("ANOTHER_RANDOM_VAR", "123")
        # extra="ignore" により例外が発生しない
        s = Settings()
        assert not hasattr(s, "totally_unknown_setting")
        assert not hasattr(s, "another_random_var")

    def test_known_and_unknown_env_vars_mixed(self, monkeypatch):
        """既知の環境変数は読み込まれ、未知のものは無視される."""
        monkeypatch.setenv("LLM_MODEL", "custom-model")
        monkeypatch.setenv("NONEXISTENT_FIELD", "ignored")
        s = Settings()
        assert s.llm_model == "custom-model"
        assert not hasattr(s, "nonexistent_field")

    def test_constructor_extra_kwargs_ignored(self):
        """コンストラクタで未知のキーワード引数が無視される."""
        s = Settings(unknown_param="ignored_value")
        assert not hasattr(s, "unknown_param")
