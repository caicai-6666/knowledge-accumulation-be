"""助理消费与交付的显式启动配置；不会修改真实环境或自动迁移数据库。"""
import os
from pathlib import Path
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from app.config.middleware import PROJECT_ROOT


class AssistantRuntimeSettings(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True,hide_input_in_errors=True)
    enabled: bool=False
    poll_seconds: float=Field(default=3,gt=0,allow_inf_nan=False)
    model_retry_seconds: float=Field(default=30,gt=0,allow_inf_nan=False)
    inbound_poll_seconds: float=Field(default=3,gt=0,allow_inf_nan=False)
    inbound_retry_seconds: float=Field(default=30,gt=0,allow_inf_nan=False)
    inbound_batch_size: int=Field(default=100,gt=0)
    heartbeat_poll_seconds: float=Field(default=30,gt=0,allow_inf_nan=False)
    reminder_poll_seconds: float=Field(default=3,gt=0,allow_inf_nan=False)
    reminder_batch_size: int=Field(default=100,gt=0)
    reminder_retry_seconds: float=Field(default=30,gt=0,allow_inf_nan=False)
    directory_cache_capacity: int=Field(default=10,gt=0)
    directory_page_size: int=Field(default=10,gt=0)
    directory_max_results: int=Field(default=1000,gt=0)
    max_concurrent_users: int=Field(default=4,gt=0)
    thinking_effort: Literal['low','medium','xhigh']='medium'
    rendering_thinking_effort: Literal['none','low','medium','xhigh']='medium'
    publication_retry_window_seconds: float=Field(default=3600,gt=0,allow_inf_nan=False)


def load_assistant_runtime_settings(env_file: Path|None=None):
    values={**dotenv_values(env_file or PROJECT_ROOT/'.env',interpolate=False),**os.environ}
    try:
        return AssistantRuntimeSettings(**{name:values.get('ASSISTANT_'+name.upper(),default)
            for name,default in AssistantRuntimeSettings().model_dump().items()})
    except (ValueError,TypeError):
        raise ValueError('助理运行配置不合法') from None
