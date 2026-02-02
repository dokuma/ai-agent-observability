"""Grafana MCP Tool — ダッシュボード・アラート操作."""

import logging
from datetime import datetime
from typing import Any

import httpx
from langchain_core.tools import BaseTool, tool

from ai_agent_monitoring.tools.base import MCPClient

logger = logging.getLogger(__name__)


class GrafanaMCPTool:
    """Grafana MCP Server 経由のダッシュボード・アラート操作ツール群.

    grafana/mcp-grafana が提供する機能をラップする。
    PromQL/LogQLの実行もGrafana MCP経由で可能。
    """

    def __init__(self, mcp_client: MCPClient):
        self.mcp_client = mcp_client

    async def list_dashboards(self) -> dict[str, Any]:
        """ダッシュボード一覧を取得."""
        logger.info("Grafana: list dashboards")
        return await self.mcp_client.call_tool("list_dashboards", {})

    async def get_dashboard_by_uid(self, uid: str) -> dict[str, Any]:
        """UIDを指定してダッシュボードの詳細を取得."""
        logger.info("Grafana: get dashboard uid=%s", uid)
        return await self.mcp_client.call_tool(
            "get_dashboard_by_uid",
            {"uid": uid},
        )

    async def get_dashboard_panels(self, uid: str) -> dict[str, Any]:
        """ダッシュボードのパネル一覧を取得."""
        logger.info("Grafana: get panels for dashboard uid=%s", uid)
        return await self.mcp_client.call_tool(
            "get_dashboard_panels",
            {"uid": uid},
        )

    async def query_prometheus(
        self,
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        step: str = "1m",
    ) -> dict[str, Any]:
        """Grafana経由でPromQLクエリを実行."""
        params: dict[str, Any] = {"query": query, "step": step}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        logger.info("Grafana: PromQL query: %s", query)
        return await self.mcp_client.call_tool("query_prometheus", params)

    async def query_loki(
        self,
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Grafana経由でLogQLクエリを実行."""
        params: dict[str, Any] = {"query": query, "limit": limit}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        logger.info("Grafana: LogQL query: %s", query)
        return await self.mcp_client.call_tool("query_loki", params)

    async def list_alert_rules(self) -> dict[str, Any]:
        """アラートルール一覧を取得."""
        logger.info("Grafana: list alert rules")
        return await self.mcp_client.call_tool("list_alert_rules", {})

    async def get_alert_rule(self, uid: str) -> dict[str, Any]:
        """特定のアラートルールを取得."""
        logger.info("Grafana: get alert rule uid=%s", uid)
        return await self.mcp_client.call_tool(
            "get_alert_rule",
            {"uid": uid},
        )

    async def get_firing_alerts(self) -> dict[str, Any]:
        """現在発火中のアラートを取得."""
        logger.info("Grafana: get firing alerts")
        return await self.mcp_client.call_tool("get_firing_alerts", {})

    async def render_panel_image(
        self,
        dashboard_uid: str,
        panel_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        width: int = 800,
        height: int = 400,
    ) -> bytes:
        """パネルをPNG画像としてレンダリング.

        Grafana Render API (/render/d-solo/) を使用。
        """
        params: dict[str, Any] = {
            "panelId": panel_id,
            "width": width,
            "height": height,
        }
        if start:
            params["from"] = str(int(start.timestamp() * 1000))
        if end:
            params["to"] = str(int(end.timestamp() * 1000))

        logger.info(
            "Grafana: render panel image dashboard=%s panel=%d",
            dashboard_uid,
            panel_id,
        )
        async with httpx.AsyncClient(timeout=self.mcp_client.timeout) as client:
            response = await client.get(
                f"{self.mcp_client.base_url}/render/d-solo/{dashboard_uid}",
                params=params,
            )
            response.raise_for_status()
            return response.content

    async def search_dashboards(self, query: str) -> dict[str, Any]:
        """ダッシュボードをキーワード検索."""
        logger.info("Grafana: search dashboards query=%s", query)
        return await self.mcp_client.call_tool(
            "search_dashboards",
            {"query": query},
        )


def create_grafana_tools(mcp_client: MCPClient) -> list[BaseTool]:
    """LangChain Tool としてラップされた Grafana ツール群を生成."""
    grafana = GrafanaMCPTool(mcp_client)

    @tool
    async def grafana_list_dashboards() -> dict[str, Any]:
        """Grafanaのダッシュボード一覧を取得します。"""
        return await grafana.list_dashboards()

    @tool
    async def grafana_get_dashboard(uid: str) -> dict[str, Any]:
        """指定UIDのGrafanaダッシュボードの詳細を取得します。"""
        return await grafana.get_dashboard_by_uid(uid)

    @tool
    async def grafana_search_dashboards(query: str) -> dict[str, Any]:
        """キーワードでGrafanaダッシュボードを検索します。"""
        return await grafana.search_dashboards(query)

    @tool
    async def grafana_query_prometheus(
        query: str,
        start: str = "",
        end: str = "",
        step: str = "1m",
    ) -> dict[str, Any]:
        """Grafana経由でPromQLクエリを実行します。start/endはISO 8601形式。"""
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        return await grafana.query_prometheus(query, s, e, step)

    @tool
    async def grafana_query_loki(
        query: str,
        start: str = "",
        end: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Grafana経由でLogQLクエリを実行します。start/endはISO 8601形式。"""
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        return await grafana.query_loki(query, s, e, limit)

    @tool
    async def grafana_list_alert_rules() -> dict[str, Any]:
        """Grafanaのアラートルール一覧を取得します。"""
        return await grafana.list_alert_rules()

    @tool
    async def grafana_get_firing_alerts() -> dict[str, Any]:
        """現在発火中のGrafanaアラートを取得します。"""
        return await grafana.get_firing_alerts()

    return [
        grafana_list_dashboards,
        grafana_get_dashboard,
        grafana_search_dashboards,
        grafana_query_prometheus,
        grafana_query_loki,
        grafana_list_alert_rules,
        grafana_get_firing_alerts,
    ]
