"""外部 MCP 工具服务连接配置；仅管理端点、超时与启停开关，不含工具业务规则。"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.middleware import PROJECT_ROOT

DEFAULT_MCP_SERVER_URL = 'http://127.0.0.1:18100/mcp'
DEFAULT_MCP_TIMEOUT_SECONDS = 360.0
DEFAULT_MCP_ENABLED = True


def _parse_bool(raw: object, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if not normalized:
            return default
        if normalized in {'1', 'true', 'yes', 'on'}:
            return True
        if normalized in {'0', 'false', 'no', 'off'}:
            return False
    raise ValueError('MCP_ENABLED 必须为布尔值（true/false）')


class MCPSettings(BaseModel):
    """MCP Streamable HTTP 客户端运行配置。"""

    model_config = ConfigDict(frozen=True, extra='forbid')

    enabled: bool = Field(default=DEFAULT_MCP_ENABLED)
    server_url: str = Field(default=DEFAULT_MCP_SERVER_URL)
    timeout_seconds: float = Field(default=DEFAULT_MCP_TIMEOUT_SECONDS, gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def validate_server_url(self) -> MCPSettings:
        url = self.server_url.strip()
        if not url:
            if self.enabled:
                raise ValueError('启用 MCP 时必须配置 MCP_SERVER_URL')
            object.__setattr__(self, 'server_url', '')
            return self
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise ValueError('MCP_SERVER_URL 必须为合法的 HTTP/HTTPS 地址')
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError('MCP_SERVER_URL 不能包含凭据或锚点片段')
        object.__setattr__(self, 'server_url', url)
        return self


def load_mcp_settings(env_file: str | Path | None = None) -> MCPSettings:
    """从 `.env` 与环境变量加载 MCP 配置，环境变量优先级高于文件。"""
    values = {**dotenv_values(env_file or PROJECT_ROOT / '.env', interpolate=False), **os.environ}
    raw_url = values.get('MCP_SERVER_URL')
    server_url = DEFAULT_MCP_SERVER_URL if raw_url is None else str(raw_url).strip()
    raw_timeout = values.get('MCP_TIMEOUT_SECONDS')
    timeout_seconds = (
        DEFAULT_MCP_TIMEOUT_SECONDS
        if raw_timeout is None or str(raw_timeout).strip() == ''
        else raw_timeout
    )
    try:
        enabled = _parse_bool(values.get('MCP_ENABLED'), default=DEFAULT_MCP_ENABLED)
        return MCPSettings(
            enabled=enabled,
            server_url=server_url,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as error:
        raise ValueError(f'MCP 配置不合法：{error}') from None
