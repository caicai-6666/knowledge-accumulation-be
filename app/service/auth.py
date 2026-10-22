"""登录与只验证两条独立入口，不在验证时更新资料或恢复离线登录。"""

import asyncio
import logging
from contextlib import asynccontextmanager

from app.auth.developer import DeveloperAuthentication
from app.auth.dingtalk import DingTalkAuthentication
from app.auth.error import AuthUnavailable, InvalidCredentials
from app.auth.model import AuthenticatedUser
from app.config.auth import AuthSettings


class AuthService:
    def __init__(self, settings: AuthSettings, backend: DeveloperAuthentication | DingTalkAuthentication):
        self.settings, self._backend = settings, backend

    @asynccontextmanager
    async def _operation(self):
        try:
            async with asyncio.timeout(self.settings.operation_timeout_seconds):
                yield
        except (AuthUnavailable, InvalidCredentials):
            raise
        except Exception as error:
            # 不打印异常原文，避免钉钉 code、用户 token 或原始响应进入日志。
            logging.getLogger(__name__).warning('用户认证操作失败：error_type=%s', type(error).__name__)
            raise AuthUnavailable('用户认证暂不可用') from None

    async def login(self, credential: str) -> tuple[str, AuthenticatedUser, int]:
        async with self._operation():
            return await self._backend.login(credential)

    async def resolve_user(self, access_token: str) -> AuthenticatedUser | None:
        """所有普通查询只请求中央 token 状态，绝不创建或恢复登录。"""
        async with self._operation():
            return await self._backend.resolve_user(access_token)

    async def resolve_current_user(self, access_token: str) -> AuthenticatedUser | None:
        return await self.resolve_user(access_token)
