"""用户中央会话适配：显式登录可以建立会话，验证路径只查询与续期。"""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from pydantic import SecretStr

from app.auth.error import AuthUnavailable
from app.auth.session import new_access_token
from app.auth.model import AuthenticatedUser
from app.infrastructure.middleware import MiddlewareClient
from app.model.middleware_user import MiddlewareUserLoginStatus
from app.repository.employee import MySQLEmployeeRepository
from app.service.middleware_session import MiddlewareSessionService
from app.service.middleware_user import MiddlewareUserSyncService

logger = logging.getLogger(__name__)


class MiddlewareUserAuthService:
    def __init__(self, sessions: MiddlewareSessionService, client: MiddlewareClient,
                 employees: MySQLEmployeeRepository, *, maintenance_lock: asyncio.Lock,
                 registration: MiddlewareUserSyncService, timeout_seconds: float = 5):
        self._sessions, self._client, self._employees = sessions, client, employees
        self._timeout = timeout_seconds
        self._maintenance_lock = maintenance_lock
        self._registration = registration

    @asynccontextmanager
    async def _request(self, *, maintenance: bool = False):
        token = None
        try:
            async with asyncio.timeout(self._timeout), AsyncExitStack() as stack:
                if maintenance:
                    # 与启动总表共用组合根注入的锁；先取维护锁，再读平台 token，不嵌套用户锁。
                    await stack.enter_async_context(self._maintenance_lock)
                token = self._sessions.get_access_token()
                if token is None:
                    raise AuthUnavailable('中间件平台会话暂不可用')
                yield token
        except asyncio.CancelledError:
            raise
        except AuthUnavailable:
            raise
        except Exception as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if status == 401 and token is not None:
                self._sessions.invalidate_access_token(token)
            logger.warning('中央用户会话操作失败：error_type=%s status=%s', type(error).__name__, status)
            if status == 403:
                raise AuthUnavailable('中间件尚未授权本平台执行全表注册或创建登录') from None
            if status == 404:
                raise AuthUnavailable('中央用户资料尚未完成注册，不能确认登录') from None
            raise AuthUnavailable('中间件用户认证暂不可用') from None

    @staticmethod
    def _profile_missing(error: httpx.HTTPStatusError) -> bool:
        # 只认既有 PUT 契约的明确资料缺失；代理/路由的普通 404 不能触发破坏性全表发布。
        if error.response.status_code != 404:
            return False
        try:
            return error.response.json() == {'detail': '用户不存在，请先同步用户信息'}
        except ValueError:
            return False

    async def _establish_registered_login_locked(self, userid: str, access_token: str, token: SecretStr) -> MiddlewareUserLoginStatus:
        """正常资料只 PUT；仅明确缺失时全表登记，并最多重试一次 PUT。调用方持维护锁。"""
        try:
            return await self._client.establish_user_login(userid, access_token, token)
        except httpx.HTTPStatusError as error:
            if not self._profile_missing(error):
                raise
            self._registration.forget_registration(userid)
        # 不能把网络故障、403 或 401 当成资料缺失，也不能用旧的成员确认跳过缺失修复。
        await self._registration.ensure_registered_locked(userid)
        try:
            return await self._client.establish_user_login(userid, access_token, token)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                self._registration.forget_registration(userid)
            raise

    async def establish_login(self, user: AuthenticatedUser) -> tuple[str, int]:
        """可信显式登录：先生成 token，中央确认绑定后才向调用方返回，不存本地会话。"""
        async with self._request(maintenance=True) as platform_token:
            access_token = new_access_token()
            status = await self._establish_registered_login_locked(user.userid, access_token, platform_token)
            return access_token, status.ttl

    async def resolve_userid(self, access_token: str) -> str | None:
        """普通请求唯一依据是中央 token 查询，不传 userid、不获取维护写锁或创建登录。"""
        async with self._request() as platform_token:
            status = await self._client.touch_user_login(access_token, platform_token)
        return status.userid

    async def authenticate_developer(self, user: AuthenticatedUser) -> tuple[str, int]:
        async with self._request(maintenance=True) as platform_token:
            # 固定开发凭据已在本地验证；模拟身份不得覆盖真实员工。
            if await self._employees.find_by_userid(user.userid) is not None:
                raise ValueError('模拟 userid 与真实员工冲突')
            access_token = new_access_token()
            status = await self._establish_registered_login_locked(user.userid, access_token, platform_token)
            return access_token, status.ttl
