"""自动记忆向量化的扫描节奏；不改变模型请求本身的并发与超时。"""
import os
from pathlib import Path
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.config.middleware import PROJECT_ROOT


class MemoryEmbeddingSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    batch_size: int = Field(default=32, ge=1, le=256)
    scan_interval_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)


def load_memory_embedding_settings(env_file: Path | None = None) -> MemoryEmbeddingSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return MemoryEmbeddingSettings(
            batch_size=values.get('MEMORY_EMBEDDING_BATCH_SIZE', '32'),
            scan_interval_seconds=values.get('MEMORY_EMBEDDING_SCAN_INTERVAL_SECONDS', '30'))
    except ValidationError:
        raise ValueError('记忆向量化配置不合法，请检查批量大小及扫描间隔') from None
