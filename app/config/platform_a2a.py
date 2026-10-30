"""跨平台工具的显式启用、响应缓冲及等待预算。"""
import os
from pathlib import Path
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.config.middleware import PROJECT_ROOT


class PlatformA2ASettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)
    enabled: bool = False
    response_buffer_seconds: float = Field(default=5, ge=0, allow_inf_nan=False)
    wait_seconds: float = Field(default=90, gt=0, allow_inf_nan=False)
    poll_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    http_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    max_concurrent_requests: int = Field(default=16, ge=1, le=100)

    @model_validator(mode='after')
    def check_budget(self):
        if self.response_buffer_seconds >= self.wait_seconds:
            raise ValueError('广播响应缓冲必须短于整轮等待预算')
        return self


def load_platform_a2a_settings(env_file: Path | None = None):
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return PlatformA2ASettings(**{k: values.get('PLATFORM_A2A_' + k.upper(), v)
                                     for k, v in PlatformA2ASettings().model_dump().items()})
    except (ValueError, TypeError):
        raise ValueError('平台 A2A 配置不合法') from None
