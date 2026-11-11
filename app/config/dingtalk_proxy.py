"""钉钉 HTTP 固定出口配置；代理凭据与应用凭据一样不进入展示和日志。"""
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class DingTalkProxySettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    dingtalk_proxy_url: SecretStr = Field(default=SecretStr(''), exclude=True, repr=False)

    @field_validator('dingtalk_proxy_url')
    @classmethod
    def validate_proxy(cls, value):
        raw = value.get_secret_value()
        if not raw:
            return value
        try:
            url = urlsplit(raw)
            if (any(c.isspace() or ord(c) < 32 for c in raw)
                    or url.scheme not in ('http', 'https') or not url.hostname
                    or url.path not in ('', '/') or url.query or url.fragment
                    or (url.port is not None and not 1 <= url.port <= 65535)):
                raise ValueError
        except ValueError:
            raise ValueError('DINGTALK_PROXY_URL 必须为空或有效的 HTTP/HTTPS 代理地址，不允许路径、查询或片段') from None
        return value
