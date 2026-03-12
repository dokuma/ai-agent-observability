"""MCP Tool クライアント — MCP Python SDK ベース."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import httpx
from httpcore import RemoteProtocolError
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    pass

# Langfuse observe デコレータ（未インストール時はno-op）
try:
    from langfuse import observe as _observe

    _LANGFUSE_OBSERVE_AVAILABLE = True
except ImportError:
    _LANGFUSE_OBSERVE_AVAILABLE = False

    def _observe(func: Any = None, **kwargs: Any) -> Any:
        """No-op fallback when langfuse is not installed."""
        if func is not None:
            return func
        return lambda f: f


logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOOL_RESULT_CHARS = 8000


def _truncate_tool_result(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """ツール結果が大きすぎる場合に切り詰める.

    テキストコンテンツのみを切り詰め、dict構造は保持する。
    """
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if len(serialized) <= max_chars:
        return result

    # テキストコンテンツのみを切り詰め（構造は保持）
    content = result.get("content", [])
    truncated_content = []
    remaining = max_chars
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if len(text) > remaining:
                truncated_content.append({"type": "text", "text": text[:remaining]})
                remaining = 0
            else:
                truncated_content.append(item)
                remaining -= len(text)
        else:
            truncated_content.append(item)

    return {
        "content": truncated_content,
        "_truncated": True,
        "_original_chars": len(serialized),
    }


# リトライ対象とする例外タイプ
_RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
    OSError,
)


def _flatten_exception_group(eg: BaseException) -> list[BaseException]:
    """ExceptionGroup を再帰的に展開しリーフ例外のリストを返す."""
    if isinstance(eg, ExceptionGroup):
        result: list[BaseException] = []
        for exc in eg.exceptions:
            result.extend(_flatten_exception_group(exc))
        return result
    return [eg]


class MCPConnectionError(Exception):
    """MCP Server への接続に失敗した場合に送出される例外."""


class MCPTimeoutError(MCPConnectionError):
    """MCP Server への接続がタイムアウトした場合に送出される例外."""


T = TypeVar("T", bound="BaseMCPTool")


class MCPClient:
    """MCP Server との通信を行うクライアント.

    MCP Python SDK を使用し、SSEトランスポート経由で
    MCPサーバーと通信する。セッション管理・初期化・
    プロトコル差異を自動的に吸収する。

    推奨: 複数のツール呼び出しを行う場合は persistent_session() を使用して
    セッションを再利用すること。
    """

    # 対向トランスポート（フォールバック用）
    _FALLBACK_TRANSPORT: ClassVar[dict[str, str]] = {
        "sse": "streamable_http",
        "streamable_http": "sse",
    }

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        *,
        transport: str = "sse",
        use_tls: bool = False,
        verify_ssl: bool = True,
        ca_bundle: str = "",
        max_tool_result_chars: int = _DEFAULT_MAX_TOOL_RESULT_CHARS,
    ):
        """MCPClientを初期化.

        Args:
            base_url: MCPサーバーのベースURL（例: http://localhost:9091）
            timeout: HTTP接続タイムアウト（秒）
            transport: MCPトランスポート種別（"sse" or "streamable_http"）
            use_tls: TLSを使用するかどうか（Trueの場合、httpをhttpsに変換）
            verify_ssl: SSL証明書を検証するかどうか
            ca_bundle: カスタムCA証明書パス（空の場合はシステムデフォルト）
            max_tool_result_chars: ツール結果の最大文字数（超過分は切り詰め）
        """
        self.base_url = base_url.rstrip("/")
        if use_tls:
            self.base_url = self.base_url.replace("http://", "https://", 1)
        self.timeout = timeout
        self._transport = transport
        self._use_tls = use_tls
        self._verify_ssl = verify_ssl
        self._ca_bundle = ca_bundle
        self._max_tool_result_chars = max_tool_result_chars
        self._persistent_session: ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._connection_context: Any = None
        self._transport_verified: bool = False

    @property
    def endpoint_url(self) -> str:
        """トランスポートに応じたエンドポイントURLを取得."""
        if self._transport == "sse":
            return f"{self.base_url}/sse"
        return f"{self.base_url}/mcp"

    @property
    def sse_url(self) -> str:
        """SSEエンドポイントURLを取得（後方互換性）."""
        return self.endpoint_url

    def _build_ssl_verify(self) -> ssl.SSLContext | bool:
        """SSL検証設定を構築."""
        verify: ssl.SSLContext | bool = self._verify_ssl
        if self._ca_bundle:
            ctx = ssl.create_default_context(cafile=self._ca_bundle)
            verify = ctx
        return verify

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[ClientSession, None]:
        """MCPセッションを確立するコンテキストマネージャー.

        トランスポート設定に応じてSSEまたはStreamable HTTP接続を開き、
        初期化を行い、セッションを返す。
        コンテキスト終了時に自動的にクリーンアップされる。

        注意: このメソッドは毎回新しい接続を作成する。
        複数のツール呼び出しには persistent_session() を推奨。

        Yields:
            初期化済みのClientSession

        Raises:
            MCPTimeoutError: 接続がタイムアウトした場合
            MCPConnectionError: 接続に失敗した場合
        """
        url = self.endpoint_url
        logger.debug("Connecting to MCP server: %s (transport=%s)", url, self._transport)

        try:
            if self._transport == "sse":
                async with self._connect_sse() as session:
                    yield session
            else:
                async with self._connect_streamable_http() as session:
                    yield session
        except McpError as e:
            logger.error(
                "MCP protocol error (url=%s, transport=%s): %s",
                url,
                self._transport,
                e,
            )
            raise MCPConnectionError(f"MCP protocol error: {url}: {e}") from e
        except ExceptionGroup as eg:
            # MCP SDK内部のTaskGroupから発生したExceptionGroupを再帰的に展開
            leaf_exceptions = _flatten_exception_group(eg)
            error_details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in leaf_exceptions)
            logger.error("MCP TaskGroup errors (url=%s): %s", url, error_details)
            for exc in leaf_exceptions:
                if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    raise MCPTimeoutError(f"MCP server connection timed out: {url}") from exc
                if isinstance(exc, RemoteProtocolError):
                    raise MCPConnectionError(f"MCP server closed connection unexpectedly: {url}: {exc}") from exc
                if isinstance(exc, (ConnectionError, OSError)):
                    raise MCPConnectionError(f"MCP server connection failed: {url}: {exc}") from exc
            raise MCPConnectionError(f"MCP server error: {url}: {error_details}") from eg
        except TimeoutError as e:
            logger.error("MCP connection timed out: %s (url=%s)", e, url)
            raise MCPTimeoutError(f"MCP server connection timed out: {url}") from e
        except RemoteProtocolError as e:
            logger.error(
                "MCP server closed connection unexpectedly (url=%s, transport=%s): %s",
                url,
                self._transport,
                e,
            )
            raise MCPConnectionError(f"MCP server closed connection unexpectedly: {url}: {e}") from e
        except (ConnectionError, OSError) as e:
            logger.error("MCP connection failed: %s: %s (url=%s)", type(e).__name__, e, url)
            raise MCPConnectionError(f"MCP server connection failed: {url}: {e}") from e

    @asynccontextmanager
    async def _connect_sse(self) -> AsyncGenerator[ClientSession, None]:
        """SSEトランスポートで接続."""
        verify = self._build_ssl_verify()
        url = self.endpoint_url

        def _on_session_created(session_id: str) -> None:
            logger.debug("SSE session created: session_id=%s url=%s", session_id, url)

        def _httpx_client_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout or httpx.Timeout(self.timeout),
                auth=auth,
                verify=verify,
                follow_redirects=True,
            )

        async with sse_client(
            url=url,
            timeout=self.timeout,
            httpx_client_factory=_httpx_client_factory,
            on_session_created=_on_session_created,
        ) as (read_stream, write_stream):
            async with ClientSession(
                read_stream=read_stream,
                write_stream=write_stream,
            ) as session:
                logger.debug("Sending MCP initialize request (SSE): url=%s", url)
                init_result = await session.initialize()
                logger.info(
                    "MCP session initialized (SSE): server=%s version=%s url=%s",
                    init_result.serverInfo.name,
                    init_result.serverInfo.version,
                    url,
                )
                yield session

    @asynccontextmanager
    async def _connect_streamable_http(self) -> AsyncGenerator[ClientSession, None]:
        """Streamable HTTPトランスポートで接続."""
        verify = self._build_ssl_verify()
        url = self.endpoint_url

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            verify=verify,
            follow_redirects=True,
        ) as http_client:
            async with streamable_http_client(
                url=url,
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(
                    read_stream=read_stream,
                    write_stream=write_stream,
                ) as session:
                    logger.debug("Sending MCP initialize request (Streamable HTTP): url=%s", url)
                    init_result = await session.initialize()
                    logger.info(
                        "MCP session initialized (Streamable HTTP): server=%s version=%s url=%s",
                        init_result.serverInfo.name,
                        init_result.serverInfo.version,
                        url,
                    )
                    yield session

    @asynccontextmanager
    async def persistent_session(self) -> AsyncGenerator[ClientSession, None]:
        """永続的なMCPセッションを確立するコンテキストマネージャー.

        このコンテキスト内では同じセッションが再利用される。
        複数のツール呼び出しを効率的に行う場合に使用。

        使用例:
            async with client.persistent_session() as session:
                result1 = await session.call_tool("tool1", {})
                result2 = await session.call_tool("tool2", {})

        Yields:
            初期化済みのClientSession
        """
        async with self.session() as session:
            yield session

    @_observe(name="mcp_call_tool", as_type="tool")
    @retry(
        retry=retry_if_exception_type((*_RETRYABLE_EXCEPTIONS, MCPConnectionError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
        before_sleep=lambda retry_state: logger.warning(
            "MCP call_tool retry attempt %d for '%s': %s",
            retry_state.attempt_number,
            retry_state.args[1] if len(retry_state.args) > 1 else "unknown",
            retry_state.outcome.exception() if retry_state.outcome else "unknown",
        ),
    )
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """MCP Server の Tool を呼び出す.

        ネットワークエラー、タイムアウト、接続エラー時に
        exponential backoff で最大3回リトライする。

        注意: この方法は毎回新しいセッションを確立するため、
        複数のツール呼び出しを行う場合は persistent_session() を使用し、
        session.call_tool() を直接呼び出すことを推奨。

        Args:
            tool_name: 呼び出すツール名
            arguments: ツールに渡す引数

        Returns:
            ツールの実行結果

        Raises:
            MCPConnectionError: リトライ後も接続に失敗した場合
            MCPTimeoutError: リトライ後もタイムアウトした場合
        """
        async with self.session() as session:
            result = await session.call_tool(tool_name, arguments or {})
            extracted = self._extract_result(result)
            return _truncate_tool_result(extracted, self._max_tool_result_chars)

    @_observe(name="mcp_call_tool_with_session", as_type="tool")
    async def call_tool_with_session(
        self,
        session: ClientSession,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """既存のセッションを使用してツールを呼び出す.

        Args:
            session: 既存のClientSession
            tool_name: 呼び出すツール名
            arguments: ツールに渡す引数

        Returns:
            ツールの実行結果
        """
        result = await session.call_tool(tool_name, arguments or {})
        extracted = self._extract_result(result)
        return _truncate_tool_result(extracted, self._max_tool_result_chars)

    async def list_tools(self) -> list[types.Tool]:
        """利用可能なツール一覧を取得."""
        async with self.session() as session:
            result = await session.list_tools()
            return list(result.tools)

    async def detect_working_transport(self) -> str | None:
        """動作するトランスポートを検出.

        設定済みトランスポートで MCP セッション初期化を試行し、
        失敗した場合は対向トランスポートへフォールバックする。

        Returns:
            検出されたトランスポート名、または両方失敗した場合は None
        """
        if self._transport_verified:
            return self._transport

        original = self._transport
        transports_to_try = [original]
        fallback = self._FALLBACK_TRANSPORT.get(original)
        if fallback:
            transports_to_try.append(fallback)

        for transport in transports_to_try:
            self._transport = transport
            try:
                async with self.session() as session:
                    await session.list_tools()
                self._transport_verified = True
                if transport != original:
                    logger.warning(
                        "Transport fallback: %s → %s (url=%s)",
                        original,
                        transport,
                        self.base_url,
                    )
                else:
                    logger.debug("Transport verified: %s (url=%s)", transport, self.base_url)
                return transport
            except (MCPConnectionError, MCPTimeoutError) as e:
                logger.warning("Transport '%s' failed for %s: %s", transport, self.base_url, e)
                continue

        # 両方失敗 — 元のトランスポートに戻す
        self._transport = original
        logger.error(
            "No working transport found for %s (tried: %s)",
            self.base_url,
            ", ".join(transports_to_try),
        )
        return None

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
            # WARNING レベルで出力（呼び出し側でエラーを処理する想定）
            logger.warning("MCP tool returned error: %s", error_text)
            return {"error": error_text}

        # 結果からコンテンツを抽出
        extracted: dict[str, Any] = {"content": []}
        for content in result.content:
            if isinstance(content, types.TextContent):
                extracted["content"].append({"type": "text", "text": content.text})
            elif isinstance(content, types.ImageContent):
                extracted["content"].append(
                    {
                        "type": "image",
                        "mimeType": content.mimeType,
                        "data": content.data,
                    }
                )
            elif isinstance(content, types.EmbeddedResource):
                extracted["content"].append(
                    {
                        "type": "resource",
                        "resource": content.resource.model_dump(),
                    }
                )

        return extracted


class BaseMCPTool:
    """MCP ツールの基底クラス.

    セッション再利用機能を共通化し、各MCPツールクラスで
    継承して使用する。

    使用例:
        class MyMCPTool(BaseMCPTool):
            async def my_method(self) -> dict[str, Any]:
                return await self._call_tool("my_tool", {"param": "value"})

        # セッション再利用
        async with tool.session_context() as ctx:
            result1 = await ctx.my_method()
            result2 = await ctx.another_method()
    """

    def __init__(self, mcp_client: MCPClient):
        """BaseMCPToolを初期化.

        Args:
            mcp_client: MCPクライアントインスタンス
        """
        self.mcp_client = mcp_client
        self._current_session: ClientSession | None = None

    @asynccontextmanager
    async def session_context(self: T) -> AsyncGenerator[T, None]:
        """セッションを再利用するコンテキストマネージャー.

        このコンテキスト内では同じSSE接続が再利用される。
        複数のツール呼び出しを効率的に行う場合に使用。

        Yields:
            セッションがバインドされた自身のインスタンス
        """
        async with self.mcp_client.session() as session:
            self._current_session = session
            try:
                yield self
            finally:
                self._current_session = None

    def _preprocess_result(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        """ツール結果の前処理フック（サブクラスでオーバーライド可能）.

        _extract_resultの後、_truncate_tool_resultの前に呼ばれる。
        デフォルトは何もせずそのまま返す。
        """
        return result

    @_observe(name="mcp_base_call_tool", as_type="tool")
    async def _call_tool(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """ツールを呼び出す（セッション再利用対応）.

        セッションが確立済みの場合は再利用し、
        そうでない場合は新規セッションを作成する。
        常に _extract_result → _preprocess_result → _truncate_tool_result の
        一貫したパイプラインを通る。

        Args:
            tool_name: 呼び出すツール名
            params: ツールに渡すパラメータ

        Returns:
            ツールの実行結果
        """
        if self._current_session:
            result = await self._current_session.call_tool(tool_name, params)
        else:
            async with self.mcp_client.session() as session:
                result = await session.call_tool(tool_name, params)

        extracted = self.mcp_client._extract_result(result)
        preprocessed = self._preprocess_result(tool_name, extracted)
        return _truncate_tool_result(preprocessed, self.mcp_client._max_tool_result_chars)


class MCPSessionManager:
    """複数のMCPクライアントのセッションを管理.

    セッションのライフサイクルを管理し、複数のツール呼び出しを
    効率的に処理する。
    """

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._active_sessions: dict[str, ClientSession] = {}
        self._context_stack: list[Any] = []

    def register(self, name: str, client: MCPClient) -> None:
        """MCPクライアントを登録."""
        self._clients[name] = client

    def get_client(self, name: str) -> MCPClient | None:
        """登録されたクライアントを取得."""
        return self._clients.get(name)

    @asynccontextmanager
    async def connect(self, client_name: str) -> AsyncGenerator[ClientSession, None]:
        """指定したクライアントのセッションを確立.

        Args:
            client_name: 接続するクライアント名

        Yields:
            初期化済みのClientSession
        """
        client = self._clients.get(client_name)
        if not client:
            raise ValueError(f"Unknown MCP client: {client_name}")

        async with client.session() as session:
            yield session

    @asynccontextmanager
    async def connect_all(self) -> AsyncGenerator[MCPSessionManager, None]:
        """全クライアントに接続してセッションを確立.

        注意: 現在の実装では各呼び出しで個別にセッションを作成する。
        将来的には並列接続と永続セッション管理を実装予定。
        """
        yield self

    async def call_tool(
        self,
        client_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """指定クライアント経由でツールを呼び出す.

        注意: この方法は毎回新しいセッションを作成する。
        複数のツール呼び出しには connect() を使用して
        セッションを再利用することを推奨。
        """
        client = self._clients.get(client_name)
        if not client:
            raise ValueError(f"Unknown MCP client: {client_name}")
        return await client.call_tool(tool_name, arguments)

    async def call_tool_with_session(
        self,
        client_name: str,
        session: ClientSession,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """既存のセッションを使用してツールを呼び出す."""
        client = self._clients.get(client_name)
        if not client:
            raise ValueError(f"Unknown MCP client: {client_name}")
        return await client.call_tool_with_session(session, tool_name, arguments)
