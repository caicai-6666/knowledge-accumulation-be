"""MySQL 异步连接配置。"""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.config.middleware import PROJECT_ROOT


class MySQLSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(default="knowledge_accumulation", min_length=1)
    user: str = Field(default="knowledge_accumulation", min_length=1)
    password: SecretStr = Field(min_length=1, exclude=True)
    connect_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    operation_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)


def load_mysql_settings(env_file: Path | None = None) -> MySQLSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / ".env", interpolate=False), **os.environ}
    try:
        return MySQLSettings(
            host=values.get("MYSQL_HOST", "127.0.0.1"),
            port=values.get("MYSQL_PORT", "3306"),
            database=values.get("MYSQL_DATABASE", "knowledge_accumulation"),
            user=values.get("MYSQL_USER", "knowledge_accumulation"),
            password=SecretStr(values.get("MYSQL_PASSWORD") or ""),
            connect_timeout_seconds=values.get("MYSQL_CONNECT_TIMEOUT_SECONDS", "5"),
            operation_timeout_seconds=values.get("MYSQL_OPERATION_TIMEOUT_SECONDS", "10"),
        )
    except ValidationError:
        raise ValueError("MySQL 配置不合法，请检查地址、端口、数据库、账号和超时配置") from None
