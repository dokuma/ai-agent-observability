"""MCP Tool Registry — MCPクライアントの一元管理とヘルスチェック."""

import logging
from dataclasses import dataclass, field

import httpx
from langchain_core.tools import BaseTool

from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.tools.base import MCPClient
from ai_agent_monitoring.tools.grafana import create_grafana_tools
from ai_agent_monitoring.tools.loki import create_loki_tools
from ai_agent_monitoring.tools.prometheus import create_prometheus_tools

logger = logging.getLogger(__name__)


@dataclass
class MCPConnection:
    """MCP Server への接続情報と状態."""

    name: str
    client: MCPClient
    healthy: bool = False


@dataclass
class ToolRegistry:
    """MCP クライアントとLangChain Toolの一元管理.

    Settings から各MCPクライアントを生成し、
    ヘルスチェック・Tool生成を統合的に管理する。
    """

    prometheus: MCPConnection
    loki: MCPConnection
    grafana: MCPConnection
    _all_connections: list[MCPConnection] = field(init=False)

    def __post_init__(self) -> None:
        self._all_connections = [self.prometheus, self.loki, self.grafana]

    @classmethod
    def from_settings(cls, settings: Settings) -> "ToolRegistry":
        """Settingsから全MCPクライアントを生成."""
        return cls(
            prometheus=MCPConnection(
                name="prometheus",
                client=MCPClient(settings.mcp_prometheus_url),
            ),
            loki=MCPConnection(
                name="loki",
                client=MCPClient(settings.mcp_loki_url),
            ),
            grafana=MCPConnection(
                name="grafana",
                client=MCPClient(settings.mcp_grafana_url),
            ),
        )

    async def health_check(self) -> dict[str, bool]:
        """全MCP Serverのヘルスチェックを実行.

        各MCPサーバーのヘルスチェックエンドポイント:
        - grafana: /healthz (専用エンドポイント)
        - prometheus: /sse (SSE接続可能かで代用)
        - loki: /sse (SSE接続可能かで代用)
        """
        # 各MCPサーバー固有のヘルスチェックエンドポイント
        health_endpoints: dict[str, str] = {
            "grafana": "/healthz",
            "prometheus": "/sse",
            "loki": "/sse",
        }

        results: dict[str, bool] = {}
        for conn in self._all_connections:
            endpoint = health_endpoints.get(conn.name, "/healthz")
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{conn.client.base_url}{endpoint}")
                    conn.healthy = response.status_code == 200
            except httpx.HTTPError:
                conn.healthy = False

            results[conn.name] = conn.healthy
            if conn.healthy:
                logger.info("MCP Server '%s' is healthy", conn.name)
            else:
                logger.warning("MCP Server '%s' is unreachable", conn.name)

        return results

    def create_all_tools(self) -> list[BaseTool]:
        """全MCP Serverから利用可能なLangChain Toolを一括生成."""
        tools: list[BaseTool] = []
        tools += create_prometheus_tools(self.prometheus.client)
        tools += create_loki_tools(self.loki.client)
        tools += create_grafana_tools(self.grafana.client)
        return tools

    def get_healthy_connections(self) -> list[MCPConnection]:
        """ヘルスチェック済みで正常な接続のみ返す."""
        return [conn for conn in self._all_connections if conn.healthy]
