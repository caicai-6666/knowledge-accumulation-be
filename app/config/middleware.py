"""中间件连接配置，环境变量优先于项目根目录 .env。"""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MiddlewareSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: HttpUrl | None = None
    platform_code: str = Field(
        default="knowledge-accumulation", pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    secret: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True)
    request_timeout_seconds: float = Field(default=40, gt=0, allow_inf_nan=False)
    retry_interval_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_connection(self):
        if self.base_url is not None and (
            self.base_url.path not in (None, "/")
            or self.base_url.query is not None or self.base_url.fragment is not None
            or self.base_url.username is not None or self.base_url.password is not None
        ):
            raise ValueError("中间件地址必须是无凭据、路径和查询参数的 HTTP/HTTPS 根地址")
        # 完全未配置时可离线开发；只填一项时阻止启动，避免误以为已启用注册。
        if (self.base_url is not None) != bool(self.secret.get_secret_value().strip()):
            raise ValueError("中间件地址与密钥必须同时配置")
        return self

    @property
    def enabled(self) -> bool:
        return self.base_url is not None


def load_middleware_settings(env_file: Path | None = None) -> MiddlewareSettings:
    # 保留密钥中的 $ 等字符，不污染进程环境；相对工作目录不影响默认配置位置。
    values = {
        **dotenv_values(env_file if env_file is not None else PROJECT_ROOT / ".env", interpolate=False),
        **os.environ,
    }
    try:
        return MiddlewareSettings(
            base_url=(values.get("MIDDLEWARE_BASE_URL") or "").strip() or None,
            platform_code=values.get("MIDDLEWARE_PLATFORM_CODE", "knowledge-accumulation"),
            secret=SecretStr(values.get("MIDDLEWARE_SECRET") or ""),
            request_timeout_seconds=values.get("MIDDLEWARE_REQUEST_TIMEOUT_SECONDS", "40"),
            retry_interval_seconds=values.get("MIDDLEWARE_RETRY_INTERVAL_SECONDS", "30"),
        )
    except ValidationError:
        # 校验异常可能包含原始输入，不向日志或启动报错泄漏密钥。
        raise ValueError("中间件配置不合法，请检查地址、密钥、平台代号和正数超时/重试间隔") from None
