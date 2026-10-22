"""钉钉可信登录确认后建立中央会话，中央成功绑定后返回用户 token。"""

import logging

from app.auth.error import AuthUnavailable
from app.auth.model import AuthenticatedUser
from app.infrastructure.dingtalk import DingTalkClient
from app.repository.employee import MySQLEmployeeRepository
from app.service.employee_onboarding import EmployeeOnboardingService
from app.service.middleware_user_auth import MiddlewareUserAuthService


class DingTalkAuthentication:
    def __init__(self, client: DingTalkClient, employees: MySQLEmployeeRepository,
                 onboarding: EmployeeOnboardingService, central: MiddlewareUserAuthService):
        self._client, self._employees = client, employees
        self._onboarding, self._central = onboarding, central

    async def _employee(self, userid: str) -> AuthenticatedUser | None:
        try:
            user = await self._employees.find_by_userid(userid)
            if user is not None and user.userid != userid:
                raise ValueError('员工查询身份与可信 userid 不一致')
            return user
        except Exception as error:
            logging.getLogger(__name__).warning('认证员工查询失败：error_type=%s', type(error).__name__)
            raise AuthUnavailable('员工目录暂不可用') from None

    async def login(self, code: str) -> tuple[str, AuthenticatedUser, int]:
        userid = await self._client.exchange_code(code)
        user = await self._employee(userid)
        if user is None:
            user = await self._onboarding.initialize(userid)
        # 新后端负责全表登记和可信登录；既有用户增量权限维护只归 registry。
        token, ttl = await self._central.establish_login(user)
        return token, user, ttl

    async def resolve_user(self, access_token: str) -> AuthenticatedUser | None:
        userid = await self._central.resolve_userid(access_token)
        if userid is None:
            return None
        return await self._employee(userid)
