"""员工头像下载、无损转码与存储配置。"""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.middleware import PROJECT_ROOT
from app.config.dingtalk_proxy import DingTalkProxySettings


class AvatarSettings(DingTalkProxySettings):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path = PROJECT_ROOT / 'data/user/avatar'
    max_download_bytes: int = Field(default=10485760, gt=0)
    max_pixels: int = Field(default=16000000, gt=0)
    timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    allowed_hosts: tuple[str, ...] = ('dingtalk.com', 'alicdn.com', 'aliyuncs.com')

    @field_validator('root', mode='before')
    @classmethod
    def resolve_root(cls, value):
        if not str(value).strip():
            raise ValueError('头像目录不能为空')
        folder = Path(value)
        return (folder if folder.is_absolute() else PROJECT_ROOT / folder).resolve()

    @field_validator('allowed_hosts')
    @classmethod
    def validate_hosts(cls, values):
        if not values or any(not value or '/' in value or ':' in value or value != value.strip() for value in values):
            raise ValueError('头像域名白名单不能为空或包含路径与端口')
        return tuple(value.lower() for value in values)


def load_avatar_settings(env_file: Path | None = None) -> AvatarSettings:
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    try:
        return AvatarSettings(dingtalk_proxy_url=values.get('DINGTALK_PROXY_URL') or '', root=values.get('AVATAR_ROOT', 'data/user/avatar'),
            max_download_bytes=values.get('AVATAR_MAX_DOWNLOAD_BYTES', '10485760'),
            max_pixels=values.get('AVATAR_MAX_PIXELS', '16000000'),
            timeout_seconds=values.get('AVATAR_TIMEOUT_SECONDS', '10'),
            allowed_hosts=tuple(item.strip() for item in values.get('AVATAR_ALLOWED_HOSTS',
                'dingtalk.com,alicdn.com,aliyuncs.com').split(',')))
    except (ValueError, TypeError):
        raise ValueError('头像配置不合法，请检查目录、大小、超时与允许域名') from None
