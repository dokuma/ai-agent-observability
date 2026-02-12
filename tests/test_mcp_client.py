"""tools/base.py の MCPClient / MCPSessionManager のテスト."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import types

from ai_agent_monitoring.tools.base import BaseMCPTool, MCPClient, MCPSessionManager


# ---------------------------------------------------------------------------
# MCPClient._extract_result
# ---------------------------------------------------------------------------
class TestExtractResult:
    """MCPClient._extract_result の正常系/エラー系."""

    def setup_method(self):
        self.client = MCPClient("http://localhost:8080")

    def test_extract_error_result(self):
        """isError=True の場合、error キーが返る."""
        text_content = types.TextContent(type="text", text="Something went wrong")
        result = types.CallToolResult(
            content=[text_content],
            isError=True,
        )
        extracted = self.client._extract_result(result)
        assert "error" in extracted
        assert extracted["error"] == "Something went wrong"

    def test_extract_error_multiple_text(self):
        """複数のTextContentがあるエラー結果."""
        contents = [
            types.TextContent(type="text", text="Error part1"),
            types.TextContent(type="text", text=" part2"),
        ]
        result = types.CallToolResult(content=contents, isError=True)
        extracted = self.client._extract_result(result)
        assert extracted["error"] == "Error part1 part2"

    def test_extract_text_content(self):
        """正常系: TextContentを抽出."""
        text_content = types.TextContent(type="text", text="Hello world")
        result = types.CallToolResult(content=[text_content], isError=False)
        extracted = self.client._extract_result(result)
        assert len(extracted["content"]) == 1
        assert extracted["content"][0]["type"] == "text"
        assert extracted["content"][0]["text"] == "Hello world"

    def test_extract_image_content(self):
        """正常系: ImageContentを抽出."""
        img_content = types.ImageContent(type="image", mimeType="image/png", data="base64data")
        result = types.CallToolResult(content=[img_content], isError=False)
        extracted = self.client._extract_result(result)
        assert len(extracted["content"]) == 1
        assert extracted["content"][0]["type"] == "image"
        assert extracted["content"][0]["mimeType"] == "image/png"
        assert extracted["content"][0]["data"] == "base64data"

    def test_extract_embedded_resource(self):
        """正常系: EmbeddedResourceを抽出."""
        resource = types.TextResourceContents(
            uri="file:///test.txt",
            text="resource data",
            mimeType="text/plain",
        )
        embedded = types.EmbeddedResource(type="resource", resource=resource)
        result = types.CallToolResult(content=[embedded], isError=False)
        extracted = self.client._extract_result(result)
        assert len(extracted["content"]) == 1
        assert extracted["content"][0]["type"] == "resource"
        assert "resource" in extracted["content"][0]

    def test_extract_empty_content(self):
        """正常系: 空のコンテンツ."""
        result = types.CallToolResult(content=[], isError=False)
        extracted = self.client._extract_result(result)
        assert extracted["content"] == []

    def test_extract_mixed_content(self):
        """正常系: 複数タイプのコンテンツが混在."""
        contents = [
            types.TextContent(type="text", text="text1"),
            types.ImageContent(type="image", mimeType="image/jpeg", data="img"),
            types.TextContent(type="text", text="text2"),
        ]
        result = types.CallToolResult(content=contents, isError=False)
        extracted = self.client._extract_result(result)
        assert len(extracted["content"]) == 3
        assert extracted["content"][0]["type"] == "text"
        assert extracted["content"][1]["type"] == "image"
        assert extracted["content"][2]["type"] == "text"


# ---------------------------------------------------------------------------
# MCPClient.call_tool (SSE接続をモック)
# ---------------------------------------------------------------------------
class TestMCPClientCallTool:
    """MCPClient.call_tool の正常系/エラー系."""

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        """正常にツール呼び出しが行われる."""
        client = MCPClient("http://localhost:8080")

        text_content = types.TextContent(type="text", text='{"status": "ok"}')
        mock_call_result = types.CallToolResult(
            content=[text_content],
            isError=False,
        )

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_call_result)
        mock_session.initialize = AsyncMock()

        with patch.object(client, "session") as mock_session_cm:
            mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await client.call_tool("my_tool", {"key": "value"})

        assert "content" in result
        assert result["content"][0]["text"] == '{"status": "ok"}'
        mock_session.call_tool.assert_called_once_with("my_tool", {"key": "value"})

    @pytest.mark.asyncio
    async def test_call_tool_error(self):
        """ツール呼び出しがエラーを返す."""
        client = MCPClient("http://localhost:8080")

        error_content = types.TextContent(type="text", text="tool error")
        mock_call_result = types.CallToolResult(
            content=[error_content],
            isError=True,
        )

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_call_result)

        with patch.object(client, "session") as mock_session_cm:
            mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await client.call_tool("failing_tool", {})

        assert "error" in result
        assert result["error"] == "tool error"

    @pytest.mark.asyncio
    async def test_call_tool_none_arguments(self):
        """arguments=None の場合、空dictが渡される."""
        client = MCPClient("http://localhost:8080")

        mock_call_result = types.CallToolResult(content=[], isError=False)
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_call_result)

        with patch.object(client, "session") as mock_session_cm:
            mock_session_cm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cm.return_value.__aexit__ = AsyncMock(return_value=None)

            await client.call_tool("tool_no_args")

        mock_session.call_tool.assert_called_once_with("tool_no_args", {})


# ---------------------------------------------------------------------------
# BaseMCPTool._call_tool  セッションあり/なし分岐
# ---------------------------------------------------------------------------
class TestBaseMCPToolCallTool:
    """BaseMCPTool._call_tool のセッションあり/なしの分岐."""

    @pytest.mark.asyncio
    async def test_call_tool_without_session(self):
        """セッションなし: mcp_client.call_tool が呼ばれる."""
        mock_client = MagicMock(spec=MCPClient)
        mock_client.call_tool = AsyncMock(return_value={"content": []})

        tool = BaseMCPTool(mock_client)
        assert tool._current_session is None

        result = await tool._call_tool("some_tool", {"param": "val"})

        mock_client.call_tool.assert_called_once_with("some_tool", {"param": "val"})
        assert result == {"content": []}

    @pytest.mark.asyncio
    async def test_call_tool_with_session(self):
        """セッションあり: session.call_tool + _extract_result が呼ばれる."""
        text_content = types.TextContent(type="text", text="result")
        mock_call_result = types.CallToolResult(
            content=[text_content],
            isError=False,
        )

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_call_result)

        mock_client = MCPClient("http://localhost:8080")

        tool = BaseMCPTool(mock_client)
        tool._current_session = mock_session

        result = await tool._call_tool("some_tool", {"key": "val"})

        mock_session.call_tool.assert_called_once_with("some_tool", {"key": "val"})
        assert "content" in result
        assert result["content"][0]["text"] == "result"

    @pytest.mark.asyncio
    async def test_session_context_sets_and_clears_session(self):
        """session_context がセッションを設定し、終了時にクリアする."""
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()

        mock_client = MagicMock(spec=MCPClient)
        mock_client.session = MagicMock()
        mock_client.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client.session.return_value.__aexit__ = AsyncMock(return_value=None)

        tool = BaseMCPTool(mock_client)
        assert tool._current_session is None

        async with tool.session_context() as ctx:
            assert ctx is tool
            assert tool._current_session is mock_session

        assert tool._current_session is None


# ---------------------------------------------------------------------------
# MCPSessionManager
# ---------------------------------------------------------------------------
class TestMCPSessionManager:
    """MCPSessionManager のテスト."""

    def test_register_and_get_client(self):
        """register でクライアントを登録し、get_client で取得."""
        manager = MCPSessionManager()
        client = MCPClient("http://localhost:8080")
        manager.register("grafana", client)

        assert manager.get_client("grafana") is client

    def test_get_client_not_found(self):
        """未登録のクライアント取得で None が返る."""
        manager = MCPSessionManager()
        assert manager.get_client("nonexistent") is None

    def test_register_overwrites(self):
        """同名で再登録すると上書きされる."""
        manager = MCPSessionManager()
        client1 = MCPClient("http://host1:8080")
        client2 = MCPClient("http://host2:8080")

        manager.register("prom", client1)
        manager.register("prom", client2)

        assert manager.get_client("prom") is client2

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """connect で正常にセッションが取得できる."""
        manager = MCPSessionManager()
        mock_session = AsyncMock()

        mock_client = MagicMock(spec=MCPClient)
        mock_client.session = MagicMock()
        mock_client.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client.session.return_value.__aexit__ = AsyncMock(return_value=None)

        manager.register("grafana", mock_client)

        async with manager.connect("grafana") as session:
            assert session is mock_session

    @pytest.mark.asyncio
    async def test_connect_unknown_client(self):
        """未登録のクライアントに接続しようとすると ValueError."""
        manager = MCPSessionManager()

        with pytest.raises(ValueError, match="Unknown MCP client: unknown"):
            async with manager.connect("unknown"):
                pass

    @pytest.mark.asyncio
    async def test_call_tool_delegates(self):
        """call_tool が対象クライアントの call_tool に委譲する."""
        manager = MCPSessionManager()
        mock_client = MagicMock(spec=MCPClient)
        mock_client.call_tool = AsyncMock(return_value={"content": []})

        manager.register("prom", mock_client)

        result = await manager.call_tool("prom", "query", {"expr": "up"})

        mock_client.call_tool.assert_called_once_with("query", {"expr": "up"})
        assert result == {"content": []}

    @pytest.mark.asyncio
    async def test_call_tool_unknown_client(self):
        """未登録クライアントでの call_tool は ValueError."""
        manager = MCPSessionManager()

        with pytest.raises(ValueError, match="Unknown MCP client: missing"):
            await manager.call_tool("missing", "tool", {})

    @pytest.mark.asyncio
    async def test_call_tool_with_session_delegates(self):
        """call_tool_with_session が対象クライアントに委譲する."""
        manager = MCPSessionManager()
        mock_session = AsyncMock()
        mock_client = MagicMock(spec=MCPClient)
        mock_client.call_tool_with_session = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})

        manager.register("loki", mock_client)

        result = await manager.call_tool_with_session("loki", mock_session, "query_logs", {"query": '{job="app"}'})

        mock_client.call_tool_with_session.assert_called_once_with(mock_session, "query_logs", {"query": '{job="app"}'})
        assert result["content"][0]["text"] == "ok"

    @pytest.mark.asyncio
    async def test_call_tool_with_session_unknown_client(self):
        """未登録クライアントでの call_tool_with_session は ValueError."""
        manager = MCPSessionManager()
        mock_session = AsyncMock()

        with pytest.raises(ValueError, match="Unknown MCP client: nope"):
            await manager.call_tool_with_session("nope", mock_session, "tool", {})

    @pytest.mark.asyncio
    async def test_connect_all(self):
        """connect_all はマネージャー自身を返す."""
        manager = MCPSessionManager()
        async with manager.connect_all() as mgr:
            assert mgr is manager
