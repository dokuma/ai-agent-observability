"""Prometheus MCP Tool — PromQL クエリ実行."""

import logging
from datetime import datetime
from typing import Any

from langchain_core.tools import BaseTool, tool

from ai_agent_monitoring.tools.base import MCPClient

logger = logging.getLogger(__name__)


class PrometheusMCPTool:
    """Prometheus MCP Server 経由の PromQL 実行ツール群."""

    def __init__(self, mcp_client: MCPClient):
        self.mcp_client = mcp_client

    async def instant_query(self, query: str, time: datetime | None = None) -> dict[str, Any]:
        """PromQL インスタントクエリを実行."""
        params: dict[str, Any] = {"query": query}
        if time:
            params["time"] = time.isoformat()

        logger.info("Prometheus instant query: %s", query)
        return await self.mcp_client.call_tool("query_prometheus", params)

    async def range_query(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step: str = "1m",
    ) -> dict[str, Any]:
        """PromQL レンジクエリを実行."""
        params = {
            "query": query,
            "type": "range",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step": step,
        }

        logger.info("Prometheus range query: %s (%s ~ %s)", query, start, end)
        return await self.mcp_client.call_tool("query_prometheus", params)

    async def get_metric_metadata(self, metric: str) -> dict[str, Any]:
        """メトリクスのメタデータを取得."""
        return await self.mcp_client.call_tool(
            "get_metric_metadata",
            {"metric": metric},
        )

    async def get_label_values(self, label: str) -> dict[str, Any]:
        """ラベルの値一覧を取得."""
        return await self.mcp_client.call_tool(
            "get_label_values",
            {"label": label},
        )


def create_prometheus_tools(mcp_client: MCPClient) -> list[BaseTool]:
    """LangChain Tool としてラップされた Prometheus ツール群を生成."""
    prom = PrometheusMCPTool(mcp_client)

    @tool
    async def query_prometheus_instant(query: str, time: str = "") -> dict[str, Any]:
        """PromQLインスタントクエリを実行します。queryにPromQL式を指定してください。"""
        t = datetime.fromisoformat(time) if time else None
        return await prom.instant_query(query, t)

    @tool
    async def query_prometheus_range(
        query: str,
        start: str,
        end: str,
        step: str = "1m",
    ) -> dict[str, Any]:
        """PromQLレンジクエリを実行します。start/endはISO 8601形式で指定してください。"""
        return await prom.range_query(
            query,
            datetime.fromisoformat(start),
            datetime.fromisoformat(end),
            step,
        )

    return [query_prometheus_instant, query_prometheus_range]
