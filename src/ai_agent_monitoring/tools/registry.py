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
from ai_agent_monitoring.tools.time import create_time_tools

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
        use_tls = settings.mcp_use_tls
        verify_ssl = settings.mcp_verify_ssl
        ca_bundle = settings.mcp_ca_bundle
        default_transport = settings.mcp_transport
        return cls(
            prometheus=MCPConnection(
                name="prometheus",
                client=MCPClient(
                    settings.mcp_prometheus_url,
                    transport=settings.mcp_prometheus_transport or default_transport,
                    use_tls=use_tls,
                    verify_ssl=verify_ssl,
                    ca_bundle=ca_bundle,
                ),
            ),
            loki=MCPConnection(
                name="loki",
                client=MCPClient(
                    settings.mcp_loki_url,
                    transport=settings.mcp_loki_transport or default_transport,
                    use_tls=use_tls,
                    verify_ssl=verify_ssl,
                    ca_bundle=ca_bundle,
                ),
            ),
            grafana=MCPConnection(
                name="grafana",
                client=MCPClient(
                    settings.mcp_grafana_url,
                    transport=settings.mcp_grafana_transport or default_transport,
                    use_tls=use_tls,
                    verify_ssl=verify_ssl,
                    ca_bundle=ca_bundle,
                ),
            ),
        )

    async def health_check(self) -> dict[str, bool]:
        """全MCP Serverのヘルスチェックを実行.

        各MCPサーバーのヘルスチェック方法:
        - grafana: GET /healthz (専用ヘルスエンドポイント、200を期待)
        - prometheus/loki: GET <base_url> (ルートパス) で応答確認
          プロトコルエンドポイント (/sse, /mcp) はヘルスチェックに不適切:
          - /sse: SSEストリーム接続が開始されハングする
          - /mcp: POST only のため GET に 405/406 を返す
          - トランスポート不一致時は 404 を返す
          ベースURLへのGETはルート未定義で 404 を返すが、
          HTTP応答自体がサーバー稼働の証拠となる。
        """
        results: dict[str, bool] = {}
        for conn in self._all_connections:
            if conn.name == "grafana":
                url = f"{conn.client.base_url}/healthz"
            else:
                # プロトコルエンドポイントではなくベースURLを使用
                url = conn.client.base_url
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(url)
                    if conn.name == "grafana":
                        conn.healthy = response.status_code == 200
                    else:
                        # HTTP応答があればサーバー稼働中（5xx以外）
                        conn.healthy = response.status_code < 500
            except httpx.HTTPError:
                conn.healthy = False

            results[conn.name] = conn.healthy
            if conn.healthy:
                logger.info("MCP Server '%s' is healthy (url=%s)", conn.name, url)
            else:
                logger.warning("MCP Server '%s' is unreachable (url=%s)", conn.name, url)

        return results

    def create_all_tools(self, healthy_only: bool = True) -> list[BaseTool]:
        """全MCP Serverから利用可能なLangChain Toolを一括生成.

        Args:
            healthy_only: Trueの場合、healthyなMCPのみからツールを生成

        Returns:
            利用可能なツールのリスト
        """
        tools: list[BaseTool] = []

        # 時刻ツールは常に追加（ローカルツールなのでヘルスチェック不要）
        tools += create_time_tools()

        if not healthy_only or self.prometheus.healthy:
            tools += create_prometheus_tools(self.prometheus.client)
        else:
            logger.warning("Prometheus MCP is unhealthy, skipping tools")

        if not healthy_only or self.loki.healthy:
            tools += create_loki_tools(self.loki.client)
        else:
            logger.warning("Loki MCP is unhealthy, skipping tools")

        if not healthy_only or self.grafana.healthy:
            tools += create_grafana_tools(self.grafana.client)
        else:
            logger.warning("Grafana MCP is unhealthy, skipping tools")

        return tools

    def create_prioritized_tools(self, grafana_first: bool = True) -> list[BaseTool]:
        """優先順位付きでツールを生成.

        Grafana MCPを優先し、unhealthyなMCPはスキップする。
        Grafana経由でPrometheus/Lokiにアクセスできる場合、
        直接のprometheus-mcp/loki-mcpはフォールバックとして使用。

        Args:
            grafana_first: Grafanaツールを優先する場合True

        Returns:
            優先順位付きのツールリスト
        """
        tools: list[BaseTool] = []

        # 時刻ツールは常に最初に追加
        tools += create_time_tools()

        if grafana_first and self.grafana.healthy:
            # Grafana MCPが健全ならGrafanaツールを優先
            tools += create_grafana_tools(self.grafana.client)
            logger.info("Grafana MCP tools added (primary)")

            # Grafana経由でアクセスできない場合のフォールバック
            if self.prometheus.healthy:
                tools += create_prometheus_tools(self.prometheus.client)
                logger.info("Prometheus MCP tools added (fallback)")

            if self.loki.healthy:
                tools += create_loki_tools(self.loki.client)
                logger.info("Loki MCP tools added (fallback)")
        else:
            # Grafanaが使えない場合は直接アクセス
            if self.grafana.healthy:
                tools += create_grafana_tools(self.grafana.client)

            if self.prometheus.healthy:
                tools += create_prometheus_tools(self.prometheus.client)
                logger.info("Prometheus MCP tools added (direct)")
            else:
                logger.warning("Prometheus MCP is unhealthy, skipping")

            if self.loki.healthy:
                tools += create_loki_tools(self.loki.client)
                logger.info("Loki MCP tools added (direct)")
            else:
                logger.warning("Loki MCP is unhealthy, skipping")

        if not tools:
            logger.error("No healthy MCP servers available!")

        return tools

    def get_healthy_connections(self) -> list[MCPConnection]:
        """ヘルスチェック済みで正常な接続のみ返す."""
        return [conn for conn in self._all_connections if conn.healthy]

    def is_any_healthy(self) -> bool:
        """少なくとも1つのMCPが健全かどうか."""
        return any(conn.healthy for conn in self._all_connections)
