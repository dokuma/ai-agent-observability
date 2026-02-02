"""テスト用の共通フィクスチャ."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.core.models import (
    Alert,
    LogEntry,
    LogsResult,
    MetricsResult,
    Severity,
    UserQuery,
)
from ai_agent_monitoring.core.state import InvestigationPlan, TimeRange
from ai_agent_monitoring.tools.base import MCPClient


@pytest.fixture
def settings() -> Settings:
    """テスト用設定."""
    return Settings(
        llm_endpoint="http://localhost:8000",
        llm_model="test-model",
        mcp_prometheus_url="http://localhost:9090",
        mcp_loki_url="http://localhost:3100",
        mcp_grafana_url="http://localhost:3000",
        langfuse_enabled=False,
    )


@pytest.fixture
def sample_alert() -> Alert:
    """テスト用アラート."""
    return Alert(
        alert_name="HighCPUUsage",
        severity=Severity.CRITICAL,
        instance="web-server-01",
        summary="CPU usage exceeds 90%",
        description="CPU has been above 90% for 5 minutes",
        labels={"alertname": "HighCPUUsage", "instance": "web-server-01"},
        annotations={"summary": "CPU usage exceeds 90%"},
        starts_at=datetime(2026, 2, 1, 16, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_user_query() -> UserQuery:
    """テスト用ユーザクエリ."""
    return UserQuery(
        raw_input="昨日の16時ごろシステムで異常がなかったか確認してください",
        time_reference="昨日の16時ごろ",
    )


@pytest.fixture
def sample_user_query_no_time() -> UserQuery:
    """時間指定なしのテスト用ユーザクエリ."""
    return UserQuery(
        raw_input="サーバの状態を確認してください",
    )


@pytest.fixture
def sample_time_range() -> TimeRange:
    """テスト用時間範囲."""
    return TimeRange(
        start=datetime(2026, 2, 1, 15, 30, 0, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, 16, 30, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_plan(sample_time_range: TimeRange) -> InvestigationPlan:
    """テスト用調査計画."""
    return InvestigationPlan(
        promql_queries=[
            "rate(node_cpu_seconds_total{mode='idle'}[5m])",
            "node_memory_MemAvailable_bytes",
        ],
        logql_queries=[
            '{job="myapp"} |= "error"',
        ],
        target_instances=["web-server-01"],
        time_range=sample_time_range,
    )


@pytest.fixture
def sample_metrics_result() -> MetricsResult:
    """テスト用メトリクス分析結果."""
    return MetricsResult(
        query="rate(node_cpu_seconds_total{mode='idle'}[5m])",
        anomalies=["CPU idle rate dropped to 5% at 16:05"],
        summary="CPU使用率が16:00〜16:10の間に95%を超過。",
    )


@pytest.fixture
def sample_logs_result() -> LogsResult:
    """テスト用ログ分析結果."""
    return LogsResult(
        query='{job="myapp"} |= "error"',
        entries=[
            LogEntry(
                timestamp=datetime(2026, 2, 1, 16, 5, 0, tzinfo=timezone.utc),
                level="error",
                message="OutOfMemoryError: Java heap space",
            ),
            LogEntry(
                timestamp=datetime(2026, 2, 1, 16, 5, 1, tzinfo=timezone.utc),
                level="error",
                message="GC overhead limit exceeded",
            ),
        ],
        error_patterns=["OutOfMemoryError", "GC overhead"],
        summary="16:05にOOMエラーが発生。GCオーバーヘッドも検出。",
    )


@pytest.fixture
def mock_mcp_client() -> MCPClient:
    """モック MCP クライアント."""
    client = MagicMock(spec=MCPClient)
    client.base_url = "http://mock-mcp:8080"
    client.timeout = 30.0
    client.call_tool = AsyncMock(return_value={"status": "ok", "data": []})
    return client


@pytest.fixture
def mock_llm() -> MagicMock:
    """モック LLM."""
    llm = MagicMock()
    response = MagicMock()
    response.content = "mock response"
    response.tool_calls = []
    llm.ainvoke = AsyncMock(return_value=response)
    llm.bind_tools = MagicMock(return_value=llm)
    return llm
