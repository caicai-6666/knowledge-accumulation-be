"""按请求读取平台信息，批量拼接 SQL 管理员展示资料；不缓存、不写权限或关系。"""

import asyncio
import logging

import httpx

from app.auth.error import AuthUnavailable
from app.infrastructure.middleware import MiddlewareClient
from app.repository.employee import MySQLEmployeeRepository
from app.schema.platform import PlatformResponse
from app.service.middleware_session import MiddlewareSessionService

logger = logging.getLogger(__name__)


class PlatformQueryService:
    def __init__(self, sessions: MiddlewareSessionService, client: MiddlewareClient,
                 employees: MySQLEmployeeRepository, *, timeout_seconds: float = 60):
        self._sessions, self._client, self._employees = sessions, client, employees
        self._timeout = timeout_seconds

    async def list_platforms(self) -> list[PlatformResponse]:
        token = self._sessions.get_access_token()
        if token is None:
            raise AuthUnavailable('中间件平台会话暂不可用')
        try:
            async with asyncio.timeout(self._timeout):
                platforms = await self._client.list_platforms(token)
                if not platforms:
                    return []
                # 一次 SQL 批量读取管理员；只为中间件返回的平台附加资料，不用 SQL 关联扩充平台目录。
                profiles = await self._employees.list_platform_admin_profiles()
                return [PlatformResponse(**platform.model_dump(), admins=profiles.get(platform.code, []))
                        for platform in platforms]
        except Exception as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if status == 401:
                # 只失效本次查询所用的平台 token，不撤销用户 token，不误清除并发取得的新平台会话。
                self._sessions.invalidate_access_token(token)
            logger.warning('平台信息查询失败：error_type=%s status=%s', type(error).__name__, status)
            raise AuthUnavailable('平台信息暂不可用') from None
