"""通用多模态 vLLM 配置，按需读取，不包含合同专属流程或推理窗口。"""

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError, field_validator, model_validator

from app.config.middleware import PROJECT_ROOT


class MLLMGenerationSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    enable_thinking: bool = True
    reasoning_effort: Literal['low','medium','xhigh'] = 'xhigh'
    temperature: float = Field(default=0.7, ge=0, allow_inf_nan=False)
    top_p: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)
    top_k: int = Field(default=20, ge=0)
    min_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    presence_penalty: float = Field(default=1.5, allow_inf_nan=False)
    repetition_penalty: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    seed: int = 3407
    max_completion_tokens: int = Field(default=8192, gt=0)


class MLLMSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    provider: Literal['vllm'] = 'vllm'
    endpoint: Literal['chat_completions'] = 'chat_completions'
    base_url: HttpUrl
    model: str = Field(default='qwen38-flash-next', min_length=1, max_length=200)
    tool_tag_enabled: bool = Field(default=True, description='是否注入工具格式说明；不改变原生工具协议和推理强度映射。')
    tool_tag_file: str = Field(default='qwen3.8-flash-next-nvfp4.txt', description='assets/tool-tag 下的单个模板文件名，同时标识模型协议。')
    api_key: SecretStr = Field(default_factory=lambda:SecretStr(''))
    timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    max_concurrent_requests: int = Field(default=20, gt=0)
    context_window_tokens: int = Field(default=262144, gt=0)
    generation: MLLMGenerationSettings = Field(default_factory=MLLMGenerationSettings)

    def thinking_template_kwargs(self, enable_thinking: bool | None = None, *, generation=None) -> dict:
        selected = generation or self.generation
        enabled = selected.enable_thinking if enable_thinking is None else enable_thinking
        result = {'enable_thinking': enabled}
        if enabled:
            # 协议由显式模板文件名选择，不从任意 served-model-name 猜测；关闭注入仍保留映射。
            profiles = {
                'deepseek-v4.1-flash.txt': {'low':50, 'medium':75, 'xhigh':100},
                'glm-5.3-flash.txt': {'low':'low', 'medium':'high', 'xhigh':'max'},
            }
            effort = selected.reasoning_effort
            result['reasoning_effort'] = profiles.get(self.tool_tag_file, {}).get(effort, effort)
        return result

    @field_validator('tool_tag_file')
    @classmethod
    def validate_tool_tag_file(cls, value):
        if (not value.strip() or value != value.strip() or value in {'.','..'}
                or any(character in value for character in ('/', '\\', '\x00'))):
            raise ValueError('工具格式模板必须是非空文件名，不能包含目录路径')
        return value

    @property
    def tool_tag_path(self) -> Path:
        return PROJECT_ROOT/'assets'/'tool-tag'/self.tool_tag_file

    @field_validator('model')
    @classmethod
    def validate_model(cls,value):
        if not value.strip() or value != value.strip():
            raise ValueError('模型名称不能为空白或包含首尾空白')
        return value

    @model_validator(mode='after')
    def validate_connection(self):
        if self.base_url.username or self.base_url.password or self.base_url.query or self.base_url.fragment:
            raise ValueError('MLLM 地址不能包含凭据、查询参数或片段')
        if self.generation.max_completion_tokens >= self.context_window_tokens:
            raise ValueError('最大生成长度必须小于上下文窗口')
        return self


def load_mllm_settings(env_file: Path | None = None) -> MLLMSettings:
    values={**dotenv_values(env_file or PROJECT_ROOT/'.env',interpolate=False),**os.environ}
    generation_defaults=MLLMGenerationSettings()
    prefix='VLLM_MLLM_'
    try:
        generation=MLLMGenerationSettings.model_validate({
            name:values.get(prefix+name.upper(),getattr(generation_defaults,name))
            for name in MLLMGenerationSettings.model_fields})
        return MLLMSettings(
            provider=values.get(prefix+'PROVIDER','vllm'),
            endpoint=values.get(prefix+'ENDPOINT','chat_completions'),
            base_url=values.get(prefix+'BASE_URL') or '',
            model=values.get(prefix+'MODEL','qwen38-flash-next'),
            tool_tag_enabled=values.get(prefix+'TOOL_TAG_ENABLED','true'),
            tool_tag_file=values.get(prefix+'TOOL_TAG_FILE','qwen3.8-flash-next-nvfp4.txt'),
            api_key=SecretStr(values.get(prefix+'API_KEY') or ''),
            timeout_seconds=values.get(prefix+'TIMEOUT_SECONDS','300'),
            max_concurrent_requests=values.get(prefix+'MAX_CONCURRENT_REQUESTS','20'),
            context_window_tokens=values.get(prefix+'CONTEXT_WINDOW_TOKENS','262144'),
            generation=generation)
    except ValidationError:
        raise ValueError('MLLM 配置不完整或不合法，请检查地址、模型、生成参数及上下文预算') from None
