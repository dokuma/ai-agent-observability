"""Loki MCP Tool — LogQL クエリ実行."""

import logging
from datetime import datetime
from typing import Any

from langchain_core.tools import BaseTool, tool

from ai_agent_monitoring.tools.base import MCPClient

logger = logging.getLogger(__name__)


class LokiMCPTool:
    """Loki MCP Server 経由の LogQL 実行ツール群."""

    def __init__(self, mcp_client: MCPClient):
        self.mcp_client = mcp_client

    async def query_logs(
        self,
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """LogQL ログクエリを実行."""
        params: dict[str, Any] = {"query": query, "limit": limit}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        logger.info("Loki log query: %s (limit=%d)", query, limit)
        return await self.mcp_client.call_tool("query_loki", params)

    async def query_metrics(
        self,
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        step: str = "1m",
    ) -> dict[str, Any]:
        """LogQL メトリクスクエリを実行."""
        params: dict[str, Any] = {"query": query, "step": step}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        logger.info("Loki metric query: %s", query)
        return await self.mcp_client.call_tool("query_loki_metrics", params)

    async def find_error_patterns(
        self,
        service: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """サービスのエラーパターンを検出（Loki Sift）."""
        params: dict[str, Any] = {"service": service}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        logger.info("Loki error pattern detection: service=%s", service)
        return await self.mcp_client.call_tool("find_error_patterns", params)


def create_loki_tools(mcp_client: MCPClient) -> list[BaseTool]:
    """LangChain Tool としてラップされた Loki ツール群を生成."""
    loki = LokiMCPTool(mcp_client)

    @tool
    async def query_loki_logs(
        query: str,
        start: str = "",
        end: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """LogQLクエリでログを検索します。start/endはISO 8601形式で指定してください。"""
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        return await loki.query_logs(query, s, e, limit)

    @tool
    async def find_service_errors(
        service: str,
        start: str = "",
        end: str = "",
    ) -> dict[str, Any]:
        """指定サービスのエラーパターンを自動検出します。"""
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        return await loki.find_error_patterns(service, s, e)

    return [query_loki_logs, find_service_errors]
