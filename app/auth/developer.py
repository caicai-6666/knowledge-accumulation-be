"""固定开发凭据仅用于显式登录；用户会话完全通过中央随机 token 查询。"""

from secrets import compare_digest

from app.auth.error import InvalidCredentials
from app.auth.model import AuthenticatedUser
from app.config.auth import AuthSettings
from app.repository.employee import MySQLEmployeeRepository
from app.service.middleware_user_auth import MiddlewareUserAuthService


class DeveloperAuthentication:
    def __init__(self, settings: AuthSettings, *, central: MiddlewareUserAuthService,
                 employees: MySQLEmployeeRepository):
        settings.require_developer_credentials()
        self._central, self._employees = central, employees
        self._token = settings.developer_access_token
        self._user = settings.simulated_user()
        if self._user is None:
            raise ValueError('开发认证只能使用开发模式身份')

    async def login(self, credential: str) -> tuple[str, AuthenticatedUser, int]:
        # 先校验固定凭据；错误凭据不查数据库、不发 HTTP。
        if not compare_digest(credential.encode(), self._token.get_secret_value().encode()):
            raise InvalidCredentials('开发登录凭据无效')
        # 固定凭据绑定服务端配置的 userid；落库后资料、权限和管理员关系均以 SQL 为准。
        user = await self._employees.find_by_userid(self._user.userid)
        if user is not None:
            if user.userid != self._user.userid:
                raise ValueError('员工查询身份与开发 userid 不一致')
            token, ttl = await self._central.establish_login(user)
            return token, user, ttl
        token, ttl = await self._central.authenticate_developer(self._user)
        return token, self._user, ttl

    async def resolve_user(self, access_token: str) -> AuthenticatedUser | None:
        userid = await self._central.resolve_userid(access_token)
        if userid is None:
            return None
        user = await self._employees.find_by_userid(userid)
        if user is not None and user.userid != userid:
            raise ValueError('员工查询身份与中央 userid 不一致')
        if user is None and userid == self._user.userid:
            return self._user
        return user
