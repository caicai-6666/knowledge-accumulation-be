"""中间件平台登录与心跳的异步 HTTP 适配。"""

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, model_validator

from app.model.platform_admin import PlatformAdminTable, PlatformAdminTableResult
from app.model.middleware_user import MiddlewareUserInfo, MiddlewareUserTableResult, MiddlewareUserLoginStatus, MiddlewareUserLoginQueryStatus
from app.model.platform import PlatformInfo


class MiddlewareSessionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    expires_in_seconds: int = Field(gt=0, strict=True)
    heartbeat_interval_seconds: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.heartbeat_interval_seconds >= self.expires_in_seconds:
            raise ValueError("心跳间隔必须小于会话有效期")
        return self


class MiddlewareLoginResponse(MiddlewareSessionResponse):
    access_token: SecretStr = Field(min_length=1, exclude=True)
    token_type: Literal["bearer"]

    @model_validator(mode="after")
    def validate_token(self):
        token = self.access_token.get_secret_value()
        if not token.strip() or token != token.strip():
            raise ValueError("令牌不得为空白或含首尾空白")
        return self


class MiddlewareClient:
    def __init__(
        self, base_url: str, *, timeout_seconds: float = 40,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", timeout=timeout_seconds,
            follow_redirects=False, trust_env=False, transport=transport,
        )

    @staticmethod
    def _validate_response(response: httpx.Response, model, platform_code: str):
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("中间件成功响应状态必须为 200")
        result = model.model_validate(response.json())
        if result.code != platform_code:
            raise ValueError("中间件响应平台与请求不一致")
        return result

    async def login(self, platform_code: str, secret: SecretStr) -> MiddlewareLoginResponse:
        response = await self._http.post(
            "middleware-service/api/platform/login",
            json={"platform_code": platform_code, "secret": secret.get_secret_value()},
        )
        return self._validate_response(response, MiddlewareLoginResponse, platform_code)

    async def heartbeat(self, platform_code: str, token: SecretStr) -> MiddlewareSessionResponse:
        # 当前契约不需要请求体，身份仅由 Bearer 令牌确定，心跳不承载用户数据。
        response = await self._http.post(
            "middleware-service/api/platform/heartbeat",
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        )
        return self._validate_response(response, MiddlewareSessionResponse, platform_code)

    async def close(self) -> None:
        await self._http.aclose()

    async def _user_login(self, method: str, access_token: str, token: SecretStr,
                          *, userid: str | None = None) -> MiddlewareUserLoginStatus:
        body = {'access_token': access_token}
        if method == 'PUT':
            body['userid'] = userid
        response = await self._http.request(method, 'middleware-service/api/user/login-cache',
            headers={'Authorization': f'Bearer {token.get_secret_value()}'}, json=body)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('用户登录操作必须返回 200')
        # 查询返回 username，写入响应不含该字段；保持各自严格的额外字段校验。
        response_model = MiddlewareUserLoginQueryStatus if method == 'POST' else MiddlewareUserLoginStatus
        result = response_model.model_validate(response.json())
        if method == 'PUT' and (result.userid != userid or not result.is_logged_in):
            raise ValueError('用户登录响应与请求不一致')
        return result

    async def touch_user_login(self, access_token: str, token: SecretStr) -> MiddlewareUserLoginStatus:
        return await self._user_login('POST', access_token, token)

    async def establish_user_login(self, userid: str, access_token: str, token: SecretStr) -> MiddlewareUserLoginStatus:
        return await self._user_login('PUT', access_token, token, userid=userid)

    async def list_platforms(self, token: SecretStr) -> list[PlatformInfo]:
        response = await self._http.get('middleware-service/api/platform/list',
            headers={'Authorization': f'Bearer {token.get_secret_value()}'})
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('平台目录必须返回 200')
        platforms = TypeAdapter(list[PlatformInfo]).validate_python(response.json())
        if len({platform.code for platform in platforms}) != len(platforms):
            raise ValueError('平台目录包含重复代号')
        return platforms

    async def replace_user_table(self, users: list[MiddlewareUserInfo], token: SecretStr) -> int:
        if len({user.userid for user in users}) != len(users):
            raise ValueError('用户总表不能包含重复 userid')
        response = await self._http.post(
            'middleware-service/api/user/full-table',
            headers={'Authorization': f'Bearer {token.get_secret_value()}'},
            # 明确选用专用 DTO，防止员工资料扩展时把头像、部门或管理身份带入中间件。
            json={'users': [user.model_dump() for user in users]},
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('用户总表同步必须返回 200')
        count = MiddlewareUserTableResult.model_validate(response.json()).count
        if count != len(users):
            raise ValueError('用户总表响应数量与提交数量不一致')
        return count

    async def replace_platform_admin_table(self, admins: dict[str, list[str]], token: SecretStr) -> int:
        table = PlatformAdminTable(admins=admins)
        response = await self._http.post('middleware-service/api/platform/admins/full-table',
            headers={'Authorization': f'Bearer {token.get_secret_value()}'}, json=table.model_dump())
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('管理员全表同步必须返回 200')
        count = PlatformAdminTableResult.model_validate(response.json()).admin_count
        if count != table.relation_count:
            raise ValueError('管理员全表响应关系数与提交数量不一致')
        return count
