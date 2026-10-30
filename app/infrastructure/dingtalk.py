"""钉钉企业内部 H5 免登；应用凭证仅在服务端使用。"""

import asyncio
import logging
from time import monotonic

import httpx
from pydantic import BaseModel, Field, SecretStr

from app.auth.error import AuthUnavailable, InvalidCredentials
from app.config.auth import AuthSettings
from app.model.dingtalk import DingTalkDepartment, DingTalkEmployee


logger = logging.getLogger(__name__)


class _ApplicationToken(BaseModel):
    accessToken: SecretStr = Field(min_length=1)
    expireIn: int = Field(gt=0, strict=True)


class _DingTalkUser(BaseModel):
    userid: str = Field(min_length=1, max_length=128, strict=True)


class DingTalkClient:
    def __init__(self, settings: AuthSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        # 先在本地拒绝缺失凭据，再创建 HTTP 客户端；与仅使用模式配置的启动同步解耦。
        settings.require_dingtalk_credentials()
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=settings.dingtalk_timeout_seconds, transport=transport, trust_env=False, follow_redirects=False)
        self._token: SecretStr | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        self._token = None
        self._expires_at = 0.0
        await self._client.aclose()

    async def _application_token(self) -> str:
        async with self._lock:
            if self._token is None or monotonic() >= self._expires_at:
                try:
                    response = await self._client.post('https://api.dingtalk.com/v1.0/oauth2/accessToken', json={
                        'appKey': self._settings.dingtalk_app_key.get_secret_value(),
                        'appSecret': self._settings.dingtalk_app_secret.get_secret_value(),
                    })
                    response.raise_for_status()
                    token = _ApplicationToken.model_validate(response.json())
                except (httpx.HTTPError, ValueError, TypeError):
                    raise AuthUnavailable("钉钉应用认证暂不可用") from None
                self._token = token.accessToken
                self._expires_at = monotonic() + token.expireIn - min(60, token.expireIn / 10)
            return self._token.get_secret_value()

    async def exchange_code(self, code: str) -> str:
        result = await self._legacy_call('/topapi/v2/user/getuserinfo', {'code': code}, code_exchange=True)
        try:
            return _DingTalkUser.model_validate(result).userid
        except (ValueError, TypeError):
            raise AuthUnavailable('钉钉免登身份响应异常') from None

    async def get_employee(self, userid: str) -> DingTalkEmployee:
        result = await self._legacy_call('/topapi/v2/user/get', {'userid': userid, 'language': 'zh_CN'})
        try:
            employee = DingTalkEmployee.model_validate(result)
            if employee.userid != userid:
                raise ValueError('用户详情与已验证身份不一致')
            return employee
        except (ValueError, TypeError):
            raise AuthUnavailable('钉钉员工资料响应异常') from None

    async def get_department(self, department_id: int) -> DingTalkDepartment:
        result = await self._legacy_call('/topapi/v2/department/get', {'dept_id': department_id, 'language': 'zh_CN'})
        try:
            department = DingTalkDepartment.model_validate(result)
            if department.dept_id != department_id:
                raise ValueError('部门详情与请求不一致')
            return department
        except (ValueError, TypeError):
            logger.warning('钉钉部门查询失败：reason=invalid_department_response department_id=%s', department_id)
            raise AuthUnavailable('钉钉部门资料响应异常') from None

    async def _legacy_call(self, path: str, body: dict, *, code_exchange: bool = False) -> dict:
        for attempt in range(2):
            token = await self._application_token()
            try:
                response = await self._client.post('https://oapi.dingtalk.com' + path,
                    params={'access_token': token}, json=body)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or type(payload.get('errcode')) is not int:
                    raise ValueError("钉钉响应格式异常")
                error_code = payload['errcode']
                if error_code == 0:
                    if not isinstance(payload.get('result'), dict):
                        raise ValueError('钉钉业务响应缺少结果对象')
                    return payload['result']
            except (httpx.HTTPError, ValueError, TypeError) as error:
                # 只记录固定接口路径和异常类型，HTTP异常原文可能含 access_token。
                logger.warning('钉钉接口请求失败：endpoint=%s error_type=%s', path, type(error).__name__)
                raise AuthUnavailable("钉钉免登服务暂不可用") from None
            # 保留数值错误码用于排查权限、限流等问题，不输出上游 errmsg 或员工资料。
            logger.warning('钉钉接口业务失败：endpoint=%s errcode=%s', path, error_code)
            if error_code in (40014, 42001, 88):
                # 应用 token 过期时只重试一次，不把服务端凭证失败归为用户凭据错误。
                async with self._lock:
                    if self._token is not None and self._token.get_secret_value() == token:
                        self._token = None
                if attempt == 0:
                    continue
                raise AuthUnavailable("钉钉应用凭证刷新失败")
            if code_exchange and error_code in (40029, 40078):
                raise InvalidCredentials("钉钉免登 code 无效或已过期")
            # 限流和应用配置错误属于上游不可用，不向调用方回显 errmsg。
            raise AuthUnavailable("钉钉免登服务暂不可用")
        raise AuthUnavailable("钉钉免登服务暂不可用")
