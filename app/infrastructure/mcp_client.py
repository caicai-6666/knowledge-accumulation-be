"""外部 MCP 工具服务客户端管理器：基于官方 MCP Streamable HTTP 复用连接与会话。"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
import logging
from typing import Any

import anyio
import httpx
from mcp import ClientSession, types as mcp_types
from mcp.client.streamable_http import streamable_http_client

from app.config.mcp import MCPSettings, load_mcp_settings

try:
    import httpx2 as _mcp_httpx
except ImportError:  # pragma: no cover - 兼容未来 mcp 切回 httpx 的版本
    _mcp_httpx = httpx  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class ConversionError(ValueError):
    """文件转换失败异常，统一供探索上传与机器人入站附件复用。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ConvertedPDF:
    """MCP `file_conversion` 解码后的 PDF 产物。"""

    content: bytes
    source_format: str
    page_count: int


class MCPClientError(RuntimeError):
    """MCP 客户端基础异常。"""


class MCPUnavailableError(MCPClientError):
    """MCP 服务未启用、连接失败或传输不可用。"""


def _is_transport_error(error: BaseException) -> bool:
    """判断异常是否属于可触发一次自动重连的传输/连接故障。"""
    transport_types: list[type[BaseException]] = [
        httpx.HTTPError,
        OSError,
        TimeoutError,
        ConnectionError,
        EOFError,
        anyio.ClosedResourceError,
        anyio.BrokenResourceError,
    ]
    if _mcp_httpx is not httpx and hasattr(_mcp_httpx, 'HTTPError'):
        transport_types.append(_mcp_httpx.HTTPError)
    if isinstance(error, tuple(transport_types)):
        return True
    # anyio / TaskGroup 包装的传输异常（如 BaseExceptionGroup / ExceptionGroup）
    if isinstance(error, BaseExceptionGroup):
        return any(_is_transport_error(sub) for sub in error.exceptions)
    return False


class MCPClientManager:
    """长驻 MCP Streamable HTTP 客户端管理器。

    - 复用单个异步 HTTP 客户端与 `ClientSession`，支持并发 `call_tool`。
    - `list_tools()` 缓存工具定义，减少重复握手开销。
    - `call_tool()` 遇到传输异常时自动重连一次并重试，避免瞬断影响业务。
    - `convert_file()` 封装 MCP `file_conversion` 工具并解码 `pdf_base64`。
    """

    def __init__(
        self,
        settings: MCPSettings | None = None,
        *,
        http_client: Any = None,
        transport_factory: Callable[..., Any] | None = None,
        session_factory: Callable[..., Any] | None = None,
    ):
        self.settings = settings if settings is not None else load_mcp_settings()
        self._external_http_client = http_client
        self._transport_factory = transport_factory or streamable_http_client
        self._session_factory = session_factory or ClientSession

        self._lifecycle_lock = asyncio.Lock()
        self._exit_stack: AsyncExitStack | None = None
        self._session: Any = None
        self._tools_cache: tuple[mcp_types.Tool, ...] | None = None
        self._generation = 0

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def is_started(self) -> bool:
        return self._session is not None

    async def __aenter__(self) -> MCPClientManager:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def _build_http_client(self) -> Any:
        timeout = self.settings.timeout_seconds
        return _mcp_httpx.AsyncClient(timeout=_mcp_httpx.Timeout(timeout, read=timeout))

    async def _open_session_locked(self) -> Any:
        stack = AsyncExitStack()
        try:
            if self._external_http_client is not None:
                http_client = self._external_http_client
            else:
                http_client = await stack.enter_async_context(self._build_http_client())
            read_stream, write_stream = await stack.enter_async_context(
                self._transport_factory(
                    self.settings.server_url,
                    http_client=http_client,
                    terminate_on_close=False,
                )
            )
            session = await stack.enter_async_context(
                self._session_factory(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self.settings.timeout_seconds,
                )
            )
            await session.initialize()
        except BaseException as error:
            await stack.aclose()
            if isinstance(error, asyncio.CancelledError):
                raise
            logger.warning(
                '连接 MCP 服务失败：url=%s error_type=%s',
                self.settings.server_url,
                type(error).__name__,
            )
            raise MCPUnavailableError('无法连接外部 MCP 工具服务，请确认服务已启动且地址可达') from error

        self._exit_stack = stack
        self._session = session
        self._generation += 1
        return session

    async def _close_session_locked(self) -> None:
        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as error:
                logger.warning('关闭 MCP 会话时出现异常：error_type=%s', type(error).__name__)

    async def start(self) -> None:
        """建立长驻 HTTP 客户端与 MCP 会话；禁用时静默跳过，已启动时幂等返回。"""
        if not self.settings.enabled:
            return
        async with self._lifecycle_lock:
            if self._session is not None:
                return
            await self._open_session_locked()

    async def aclose(self) -> None:
        """释放 MCP 会话、HTTP 客户端及工具列表缓存。"""
        async with self._lifecycle_lock:
            self._tools_cache = None
            await self._close_session_locked()

    async def _ensure_session(self) -> tuple[Any, int]:
        if not self.settings.enabled:
            raise MCPUnavailableError('MCP 工具服务未启用')
        session = self._session
        if session is not None:
            return session, self._generation
        async with self._lifecycle_lock:
            if self._session is None:
                await self._open_session_locked()
            return self._session, self._generation

    async def _reconnect(self, *, failed_generation: int) -> tuple[Any, int]:
        """传输异常后重连；若已被其他并发请求完成重连，则直接复用新会话。"""
        if not self.settings.enabled:
            raise MCPUnavailableError('MCP 工具服务未启用')
        async with self._lifecycle_lock:
            if self._session is not None and self._generation != failed_generation:
                return self._session, self._generation
            await self._close_session_locked()
            session = await self._open_session_locked()
            return session, self._generation

    async def list_tools(self, *, refresh: bool = False) -> tuple[mcp_types.Tool, ...]:
        """拉取 MCP 工具元数据并缓存；支持显式刷新及传输异常自动重连一次。"""
        if not self.settings.enabled:
            raise MCPUnavailableError('MCP 工具服务未启用')
        if not refresh and self._tools_cache is not None:
            return self._tools_cache

        session, generation = await self._ensure_session()
        try:
            result = await session.list_tools()
        except Exception as error:
            if not _is_transport_error(error):
                raise MCPUnavailableError('获取 MCP 工具列表失败') from error
            logger.warning('MCP list_tools 传输异常，尝试自动重连一次：error_type=%s', type(error).__name__)
            session, _ = await self._reconnect(failed_generation=generation)
            try:
                result = await session.list_tools()
            except Exception as retry_error:
                raise MCPUnavailableError('获取 MCP 工具列表失败：重连后仍不可用') from retry_error

        tools = tuple(result.tools)
        self._tools_cache = tools
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        """调用指定 MCP 工具；传输异常时自动重连并重试一次。"""
        if not self.settings.enabled:
            raise MCPUnavailableError('MCP 工具服务未启用')
        payload = dict(arguments) if arguments else {}

        session, generation = await self._ensure_session()
        try:
            return await session.call_tool(
                name,
                payload,
                read_timeout_seconds=self.settings.timeout_seconds,
            )
        except Exception as error:
            if not _is_transport_error(error):
                raise MCPUnavailableError(f'调用 MCP 工具 {name} 失败') from error
            logger.warning(
                'MCP call_tool 传输异常，尝试自动重连一次：tool=%s error_type=%s',
                name,
                type(error).__name__,
            )
            session, _ = await self._reconnect(failed_generation=generation)
            try:
                return await session.call_tool(
                    name,
                    payload,
                    read_timeout_seconds=self.settings.timeout_seconds,
                )
            except Exception as retry_error:
                raise MCPUnavailableError(f'调用 MCP 工具 {name} 失败：重连后仍不可用') from retry_error

    async def convert_file(
        self,
        content_bytes: bytes,
        filename: str | None = None,
        *,
        operation: str = 'auto',
    ) -> ConvertedPDF:
        """调用 MCP `file_conversion` 工具转换或压缩文件，解码并返回 `ConvertedPDF`。"""
        if not isinstance(content_bytes, (bytes, bytearray)) or not content_bytes:
            raise ConversionError('empty_file', '上传文件内容为空，无法转换')

        arguments: dict[str, Any] = {
            'content_base64': base64.b64encode(bytes(content_bytes)).decode('ascii'),
            'operation': operation,
        }
        if isinstance(filename, str) and filename.strip():
            arguments['filename'] = filename.strip()

        try:
            result = await self.call_tool('file_conversion', arguments)
        except MCPUnavailableError:
            raise ConversionError('converter_unavailable', '文档转换服务暂不可用，请稍后重试') from None

        structured = result.structured_content if isinstance(result.structured_content, dict) else {}
        if result.is_error:
            error_code = str(structured.get('error_code') or 'conversion_failed')
            reason = str(structured.get('reason') or '').strip()
            if not reason:
                text_parts = [
                    block.text.strip()
                    for block in (result.content or ())
                    if isinstance(block, mcp_types.TextContent) and block.text.strip()
                ]
                reason = text_parts[0] if text_parts else '文件转换失败，请检查文件格式后重试'
            raise ConversionError(error_code, reason)

        pdf_base64 = structured.get('pdf_base64')
        if not isinstance(pdf_base64, str) or not pdf_base64.strip():
            raise ConversionError('conversion_failed', '文档转换服务未返回有效的 PDF 内容')
        try:
            pdf_bytes = base64.b64decode(pdf_base64, validate=True)
        except Exception:
            raise ConversionError('conversion_failed', '文档转换服务返回的 PDF 编码无效') from None

        source_format = str(structured.get('source_format') or 'unknown')
        raw_page_count = structured.get('page_count', 0)
        page_count = int(raw_page_count) if isinstance(raw_page_count, int) and raw_page_count >= 0 else 0
        return ConvertedPDF(
            content=pdf_bytes,
            source_format=source_format,
            page_count=page_count,
        )
