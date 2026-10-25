"""文件检索配置；权威来源只读，派生检索数据仅驻留内存。"""
import os
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from app.config.database import MySQLSettings, load_mysql_settings
from app.config.middleware import PROJECT_ROOT


class FileRetrievalSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    top_k: int = Field(default=10, ge=1)
    candidate_multiplier: int = Field(default=4, ge=1)
    cache_max_queries: int = Field(default=10, ge=1)
    cache_max_vectors: int = Field(default=256, ge=1)
    page_size: int = Field(default=5, ge=1)
    max_rounds: int = Field(default=16, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    history_weight: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)


def load_file_retrieval_settings(env_file=None):
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return FileRetrievalSettings(**{name: values['FILE_RETRIEVAL_'+name.upper()]
            for name in FileRetrievalSettings.model_fields if 'FILE_RETRIEVAL_'+name.upper() in values})
    except ValueError:
        raise ValueError('文件检索配置不合法，请检查候选数量、分页、容量和排名配置') from None


def load_file_source_settings(env_file=None):
    """显式指定权威schema；未填写连接项时复用本项目连接配置，不读取其他项目.env。"""
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    base = load_mysql_settings(env_file)
    fields = ('host', 'port', 'database', 'user', 'connect_timeout_seconds', 'operation_timeout_seconds')
    data = {name: values.get('FILE_SOURCE_MYSQL_'+name.upper()) or getattr(base, name) for name in fields}
    data['database'] = values.get('FILE_SOURCE_MYSQL_DATABASE') or 'registry'
    data['password'] = SecretStr(values.get('FILE_SOURCE_MYSQL_PASSWORD') or base.password.get_secret_value())
    try:
        return MySQLSettings(**data)
    except ValueError:
        raise ValueError('注册中心文件只读数据库配置不合法') from None
