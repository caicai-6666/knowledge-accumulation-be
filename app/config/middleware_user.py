"""用户/管理员启动全表注册的超时与重试配置，不另设启停开关。"""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from app.config.middleware import PROJECT_ROOT


class MiddlewareUserSyncSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    operation_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    retry_interval_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)


def load_middleware_user_sync_settings(env_file: Path | None = None) -> MiddlewareUserSyncSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return MiddlewareUserSyncSettings(
            operation_timeout_seconds=values.get('MIDDLEWARE_USER_SYNC_OPERATION_TIMEOUT_SECONDS', '30'),
            retry_interval_seconds=values.get('MIDDLEWARE_USER_SYNC_RETRY_INTERVAL_SECONDS', '30'),
        )
    except (ValueError, TypeError):
        raise ValueError('启动用户总表同步配置不合法，请检查超时和重试间隔') from None
