"""显式登录的严格请求响应契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.auth.model import AuthenticatedUser


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: SecretStr = Field(min_length=1, max_length=2048, description='钉钉 H5 返回的一次性免登 code，不接受客户端 userid。')

    @field_validator('code')
    @classmethod
    def reject_blank(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError('钉钉 code 不能为空白')
        return value


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    access_token: str = Field(min_length=1, repr=False, description='已在中央成功绑定的随机用户 token，不是开发固定凭据、钉钉或平台会话凭据。')
    token_type: Literal['Bearer'] = 'Bearer'
    expires_in: int = Field(gt=0, description='中央绑定确认的剩余秒数；有效查询由中央续期，不存在本地固定 TTL。')
    user: AuthenticatedUser = Field(description='当前员工身份与权限。')


class AuthErrorResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    detail: str = Field(description='脱敏错误说明，不包含请求 code、token 或上游响应。')
