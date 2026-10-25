"""注册中心文件原件读取配置；凭据来自当前用户，不设置平台密钥。"""
import os
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from app.config.middleware import PROJECT_ROOT


class RegistryFileSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    base_url: HttpUrl | None = None
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_download_bytes: int = Field(default=52428800, gt=0)

    @model_validator(mode='after')
    def root_address(self):
        u = self.base_url
        if u is not None and (u.username is not None or u.password is not None or u.query is not None
                or u.fragment is not None or u.path not in (None,'/')):
            raise ValueError('注册中心地址须为无凭据、路径和查询参数的HTTP/HTTPS根地址')
        return self


def load_registry_file_settings(env_file=None):
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return RegistryFileSettings(base_url=values.get('REGISTRY_BASE_URL') or None,
            timeout_seconds=values.get('REGISTRY_FILE_TIMEOUT_SECONDS','60'),
            max_download_bytes=values.get('REGISTRY_FILE_MAX_DOWNLOAD_BYTES','52428800'))
    except ValueError:
        raise ValueError('注册中心文件读取配置不合法，请检查根地址、超时及下载字节上限') from None
