"""进程级平台会话，负责后台注册登录、心跳与故障恢复。"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx
from pydantic import SecretStr

from app.config.middleware import MiddlewareSettings
from app.infrastructure.middleware import MiddlewareClient

logger = logging.getLogger(__name__)


class MiddlewareSessionService:
    def __init__(
        self, settings: MiddlewareSettings, *, client: MiddlewareClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._settings = settings
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._token: SecretStr | None = None
        self._expires_at = 0.0
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("中间件会话服务已关闭")
        if self._task is not None:
            return
        if not self._settings.enabled:
            logger.info("中间件地址或密钥未配置，未启用平台注册与心跳")
            return
        # 网络故障不阻塞 Web 服务启动，所有会话操作由单个后台任务串行执行。
        self._task = asyncio.create_task(self._run(), name="middleware-session")

    async def _run(self) -> None:
        settings = self._settings
        while True:
            stage = "heartbeat" if self.get_access_token() is not None else "login"
            if stage == "login":
                self._token = None
            try:
                if self._client is None:
                    self._client = MiddlewareClient(
                        str(settings.base_url), timeout_seconds=settings.request_timeout_seconds,
                    )
                started = self._clock()
                # 总超时覆盖整个请求，避免慢响应持续占用后台任务。
                async with asyncio.timeout(settings.request_timeout_seconds):
                    if stage == "login":
                        result = await self._client.login(settings.platform_code, settings.secret)
                    else:
                        result = await self._client.heartbeat(settings.platform_code, self._token)
                if stage == "login":
                    self._token = result.access_token
                    logger.info("中间件平台注册登录成功")
                # 从请求开始保守估算有效期，不能将网络耗时当成额外续期。
                self._expires_at = started + result.expires_in_seconds
                delay = min(result.heartbeat_interval_seconds, max(0, self._expires_at - self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status == 401:
                    self._token = None
                    self._expires_at = 0.0
                # 响应正文和异常原文可能含凭据；日志仅记录阶段、类型及状态码。
                logger.warning(
                    "中间件会话请求失败，将重试：stage=%s error_type=%s status=%s",
                    stage, type(exc).__name__, status,
                )
                delay = settings.retry_interval_seconds
                if self._token is not None:
                    delay = min(delay, max(0, self._expires_at - self._clock()))
            await self._sleep(delay)

    def get_access_token(self) -> SecretStr | None:
        """仅向内部服务提供有效令牌，读取不续期，禁止输出令牌明文。"""
        if self._closed or self._token is None or self._clock() >= self._expires_at:
            return None
        return self._token

    def invalidate_access_token(self, token: SecretStr) -> None:
        """仅失效当前请求使用的令牌，避免迟到的 401 清除重新登录后的令牌。"""
        if self._token == token:
            self._token = None
            self._expires_at = 0.0

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._token = None
        self._expires_at = 0.0
        if self._client is not None:
            await self._client.close()
            self._client = None
