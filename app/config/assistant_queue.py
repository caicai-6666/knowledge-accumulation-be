"""队列传输边界；只提供设置，不启动轮询或加载机器人配置。"""
import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field
from app.config.middleware import PROJECT_ROOT


class AssistantQueueSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)
    timeout_seconds: float = Field(default=40, gt=0, allow_inf_nan=False)
    max_files: int = Field(default=10, gt=0)
    max_file_bytes: int = Field(default=20971520, gt=0)
    max_response_bytes: int = Field(default=1048576, gt=0)


def load_assistant_queue_settings(env_file: Path | None = None) -> AssistantQueueSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return AssistantQueueSettings(**{name: values.get('ASSISTANT_QUEUE_' + name.upper(), default)
            for name, default in AssistantQueueSettings().model_dump().items()})
    except (ValueError, TypeError):
        raise ValueError('助理队列配置不合法，请检查超时、附件和响应上限') from None
