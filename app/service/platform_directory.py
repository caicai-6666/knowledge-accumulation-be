"""员工初始化和Agent平台查看共用的轻量目录读取；只读、无缓存。"""

import asyncio
import logging

import httpx

from app.auth.error import AuthUnavailable
from app.infrastructure.middleware import MiddlewareClient
from app.model.platform import MiddlewarePlatformInfo
from app.service.middleware_session import MiddlewareSessionService


class PlatformDirectoryService:
    def __init__(self, sessions: MiddlewareSessionService, client: MiddlewareClient, *, timeout_seconds: float = 5):
        self._sessions, self._client, self._timeout = sessions, client, timeout_seconds

    async def list_platforms(self) -> list[MiddlewarePlatformInfo]:
        token = self._sessions.get_access_token()
        if token is None:
            raise AuthUnavailable('中间件平台会话暂不可用')
        try:
            async with asyncio.timeout(self._timeout):
                return await self._client.list_platforms(token)
        except Exception as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if status == 401:
                self._sessions.invalidate_access_token(token)
            logging.getLogger(__name__).warning('平台目录读取失败：error_type=%s status=%s', type(error).__name__, status)
            raise AuthUnavailable('平台目录暂不可用') from None
