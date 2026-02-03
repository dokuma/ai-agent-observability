"""MCP Tool クライアント — MCP Python SDK ベース."""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)


class MCPClient:
    """MCP Server との通信を行うクライアント.

    MCP Python SDK を使用し、SSEトランスポート経由で
    MCPサーバーと通信する。セッション管理・初期化・
    プロトコル差異を自動的に吸収する。
    """

    def __init__(self, base_url: str, timeout: float = 30.0):
        """MCPClientを初期化.

        Args:
            base_url: MCPサーバーのベースURL（例: http://localhost:9091）
            timeout: HTTP接続タイムアウト（秒）
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session: ClientSession | None = None

    @property
    def sse_url(self) -> str:
        """SSEエンドポイントURLを取得."""
        return f"{self.base_url}/sse"

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[ClientSession, None]:
        """MCPセッションを確立するコンテキストマネージャー.

        SSE接続を開き、初期化を行い、セッションを返す。
        コンテキスト終了時に自動的にクリーンアップされる。

        Yields:
            初期化済みのClientSession
        """
        logger.info("Connecting to MCP server: %s", self.sse_url)

        async with sse_client(
            url=self.sse_url,
            timeout=self.timeout,
        ) as (read_stream, write_stream):
            async with ClientSession(
                read_stream=read_stream,
                write_stream=write_stream,
            ) as session:
                # MCPプロトコルの初期化
                init_result = await session.initialize()
                logger.info(
                    "MCP session initialized: server=%s version=%s",
                    init_result.serverInfo.name,
                    init_result.serverInfo.version,
                )
                yield session

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """MCP Server の Tool を呼び出す.

        注意: この方法は毎回新しいセッションを確立するため、
        複数のツール呼び出しを行う場合は session() コンテキスト
        マネージャーを直接使用することを推奨。

        Args:
            tool_name: 呼び出すツール名
            arguments: ツールに渡す引数

        Returns:
            ツールの実行結果
        """
        async with self.session() as session:
            result = await session.call_tool(tool_name, arguments or {})
            return self._extract_result(result)

    async def list_tools(self) -> list[types.Tool]:
        """利用可能なツール一覧を取得."""
        async with self.session() as session:
            result = await session.list_tools()
            return list(result.tools)

    def _extract_result(self, result: types.CallToolResult) -> dict[str, Any]:
        """CallToolResultからデータを抽出.

        Args:
            result: MCPツール呼び出し結果

        Returns:
            抽出されたデータ（テキストまたはJSON）
        """
        if result.isError:
            error_text = ""
            for content in result.content:
                if isinstance(content, types.TextContent):
                    error_text += content.text
            logger.error("MCP tool error: %s", error_text)
            return {"error": error_text}

        # 結果からコンテンツを抽出
        extracted: dict[str, Any] = {"content": []}
        for content in result.content:
            if isinstance(content, types.TextContent):
                extracted["content"].append({"type": "text", "text": content.text})
            elif isinstance(content, types.ImageContent):
                extracted["content"].append({
                    "type": "image",
                    "mimeType": content.mimeType,
                    "data": content.data,
                })
            elif isinstance(content, types.EmbeddedResource):
                extracted["content"].append({
                    "type": "resource",
                    "resource": content.resource.model_dump(),
                })

        return extracted


class MCPSessionManager:
    """複数のMCPクライアントのセッションを管理.

    長期間セッションを維持し、複数のツール呼び出しを
    効率的に処理する。
    """

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._sessions: dict[str, ClientSession] = {}

    def register(self, name: str, client: MCPClient) -> None:
        """MCPクライアントを登録."""
        self._clients[name] = client

    @asynccontextmanager
    async def connect_all(self) -> AsyncGenerator["MCPSessionManager", None]:
        """全クライアントに接続してセッションを確立.

        TODO: 現在の実装は単一セッションの確立を想定。
        複数セッションの並行管理は将来の拡張。
        """
        # Note: anyioのタスクグループで並列接続するなど
        # 複雑なライフサイクル管理が必要な場合は別途実装
        yield self

    async def call_tool(
        self,
        client_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """指定クライアント経由でツールを呼び出す."""
        client = self._clients.get(client_name)
        if not client:
            raise ValueError(f"Unknown MCP client: {client_name}")
        return await client.call_tool(tool_name, arguments)
