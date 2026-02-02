"""MCP Tool クライアント."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class MCPClient:
    """MCP Server との通信を行う汎用HTTPクライアント."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """MCP Server の Tool を呼び出す."""
        payload = {
            "tool": tool_name,
            "parameters": params,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/tools/call",
                json=payload,
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
