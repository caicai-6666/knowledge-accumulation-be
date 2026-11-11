"""用户身份与登录配置；登录随已配置的中间件接入装配，不另设开关。"""

import os
import json
from pathlib import Path
from typing import Annotated, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.config.middleware import PROJECT_ROOT
from app.config.dingtalk_proxy import DingTalkProxySettings
from app.auth.model import AuthenticatedUser
from app.infrastructure.avatar import avatar_response_filename


class AuthSettings(DingTalkProxySettings):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["development", "production"] = "development"
    developer_access_token: SecretStr = Field(default=SecretStr(''), exclude=True)
    operation_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    middleware_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    developer_userid: str = Field(default="developer", min_length=1, max_length=128, strict=True)
    developer_name: str = Field(default='开发模拟用户', min_length=1, max_length=200, strict=True)
    developer_department_id: int = Field(default=0, ge=0)
    developer_department_name: str = Field(default='开发模拟部门', min_length=1, max_length=200, strict=True)
    developer_avatar_path: str | None = Field(default=None, strict=True,
        pattern=r'^user/avatar/[a-f0-9]{32}\.webp$')
    developer_permissions: dict[Annotated[str, Field(pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')],
                                Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=lambda: {'registry': 0})
    developer_managed_platforms: list[Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')]] = Field(default_factory=list)
    dingtalk_corp_id: str = ""
    dingtalk_app_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    dingtalk_app_secret: SecretStr = Field(default=SecretStr(""), exclude=True)
    dingtalk_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)

    def simulated_user(self) -> AuthenticatedUser | None:
        if self.mode != 'development':
            return None
        return AuthenticatedUser(userid=self.developer_userid, name=self.developer_name,
            department_id=self.developer_department_id, department_name=self.developer_department_name,
            avatar_url=avatar_response_filename(self.developer_avatar_path),
            platform_permissions=self.developer_permissions, managed_platforms=self.developer_managed_platforms)

    @model_validator(mode="after")
    def validate_simulated_identity(self):
        if not self.developer_userid.strip() or self.developer_userid != self.developer_userid.strip():
            raise ValueError("开发者 userid 不能为空或包含首尾空白")
        if self.mode == "development":
            # 构造同一身份契约提前校验姓名、头像和权限，避免启动任务携带半合法资料。
            self.simulated_user()
        return self

    def require_developer_credentials(self) -> None:
        token = self.developer_access_token.get_secret_value()
        if not token or len(token) > 4096 or any(not ('!' <= char <= '~') for char in token):
            raise ValueError('开发登录必须配置1–4096 位无空白可打印 ASCII 固定登录凭据')

    def require_dingtalk_credentials(self) -> None:
        if not all((self.dingtalk_corp_id.strip(), self.dingtalk_app_key.get_secret_value().strip(),
                    self.dingtalk_app_secret.get_secret_value().strip())):
            raise ValueError('启用钉钉登录必须配置非空 CorpId、AppKey 和 AppSecret')


def load_auth_settings(env_file: Path | None = None) -> AuthSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / ".env", interpolate=False), **os.environ}
    try:
        return AuthSettings(dingtalk_proxy_url=values.get('DINGTALK_PROXY_URL') or '',
            mode=values.get("APP_ENV", "development"),
            developer_access_token=values.get('AUTH_DEVELOPER_ACCESS_TOKEN') or '',
            operation_timeout_seconds=values.get('AUTH_OPERATION_TIMEOUT_SECONDS', '60'),
            middleware_timeout_seconds=values.get('AUTH_MIDDLEWARE_TIMEOUT_SECONDS', '5'),
            developer_userid=values.get("AUTH_DEVELOPER_USERID", "developer"),
            developer_name=values.get('AUTH_DEVELOPER_NAME', '开发模拟用户'),
            developer_department_id=values.get('AUTH_DEVELOPER_DEPARTMENT_ID', '0'),
            developer_department_name=values.get('AUTH_DEVELOPER_DEPARTMENT_NAME', '开发模拟部门'),
            developer_avatar_path=values.get('AUTH_DEVELOPER_AVATAR_PATH') or None,
            developer_permissions=json.loads(values.get('AUTH_DEVELOPER_PERMISSIONS', '{"registry":0}')),
            developer_managed_platforms=json.loads(values.get('AUTH_DEVELOPER_MANAGED_PLATFORMS', '[]')),
            dingtalk_corp_id=values.get("DINGTALK_CORP_ID") or "",
            dingtalk_app_key=values.get("DINGTALK_APP_KEY") or "",
            dingtalk_app_secret=values.get("DINGTALK_APP_SECRET") or "",
            dingtalk_timeout_seconds=values.get("DINGTALK_TIMEOUT_SECONDS", "10"),
        )
    except (ValueError, TypeError):
        # 配置异常不携带 Pydantic 原始输入，避免密钥进入启动日志。
        raise ValueError("用户基础配置不合法，请检查 APP_ENV、模拟身份和钉钉凭据") from None
