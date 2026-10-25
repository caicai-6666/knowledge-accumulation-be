"""vLLM 的 OpenAI 兼容 embedding 配置。"""

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError, field_validator, model_validator

from app.config.middleware import PROJECT_ROOT


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: Literal["vllm"] = "vllm"
    endpoint: Literal["embeddings"] = "embeddings"
    base_url: HttpUrl
    model: str = Field(default='qwen3-vl-embedding-8b', min_length=1, max_length=200)
    dimensions: int = Field(default=4096, ge=1, le=4096)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    batch_size: int = Field(default=32, ge=1, le=2048)
    max_concurrent_requests: int = Field(default=10, ge=1)
    normalize: bool = True

    @field_validator('model')
    @classmethod
    def validate_model(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError('模型名称不能为空白或包含首尾空白')
        return value

    @model_validator(mode="after")
    def validate_address(self):
        if self.base_url.username or self.base_url.password or self.base_url.query or self.base_url.fragment:
            raise ValueError("embedding 地址不能包含凭据、查询参数或片段")
        return self


def load_embedding_settings(env_file: Path | None = None) -> EmbeddingSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / ".env", interpolate=False), **os.environ}
    try:
        return EmbeddingSettings(
            provider=values.get("VLLM_EMBEDDING_PROVIDER", "vllm"),
            endpoint=values.get("VLLM_EMBEDDING_ENDPOINT", "embeddings"),
            base_url=values.get("VLLM_EMBEDDING_BASE_URL") or "",
            model=values.get("VLLM_EMBEDDING_MODEL", "qwen3-vl-embedding-8b"),
            dimensions=values.get("VLLM_EMBEDDING_DIMENSIONS", "4096"),
            api_key=SecretStr(values.get("VLLM_EMBEDDING_API_KEY") or ""),
            timeout_seconds=values.get("VLLM_EMBEDDING_TIMEOUT_SECONDS", "60"),
            batch_size=values.get("VLLM_EMBEDDING_BATCH_SIZE", "32"),
            max_concurrent_requests=values.get("VLLM_EMBEDDING_MAX_CONCURRENT_REQUESTS", "10"),
            normalize=values.get("VLLM_EMBEDDING_NORMALIZE", "true"),
        )
    except ValidationError:
        raise ValueError("embedding 配置不完整或不合法，请检查地址、模型、维度及超时") from None


def load_optional_embedding_settings(env_file: Path | None = None) -> EmbeddingSettings | None:
    """未配置地址时不启动自动加工；已配置但非法时明确失败，不静默忽略。"""
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    if not (values.get('VLLM_EMBEDDING_BASE_URL') or '').strip():
        return None
    return load_embedding_settings(env_file)
