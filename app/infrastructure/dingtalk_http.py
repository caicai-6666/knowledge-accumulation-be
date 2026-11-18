"""钉钉 HTTP 客户端：显式代理、严格 TLS、隔离底层敏感请求日志。"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
import logging

import httpx
from pydantic import SecretStr

_private_http = ContextVar('dingtalk_private_http', default=False)


class _PrivateHTTPFilter(logging.Filter):
    def filter(self, record):
        return not _private_http.get()


class DingTalkHTTPClient(httpx.AsyncClient):
    def __init__(self, *, proxy_url: SecretStr, pinned_download: bool = False, **kwargs):
        # 显式代理适用于该客户端全部 HTTP 请求；失败直接上抛，绝不重建直连客户端。
        proxy = proxy_url.get_secret_value() or None
        if pinned_download and proxy:
            from app.infrastructure.pinned_proxy import PinnedProxyTransport
            kwargs['transport'] = PinnedProxyTransport(proxy)
            # 专用transport本身执行显式代理，禁止httpx再以默认代理覆盖它。
            proxy = None
        super().__init__(proxy=proxy,
                         trust_env=False, follow_redirects=False, verify=True, **kwargs)
        for name in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2',
                     'httpcore.proxy', 'httpcore.socks'):
            logger = logging.getLogger(name)
            if not any(isinstance(item, _PrivateHTTPFilter) for item in logger.filters):
                logger.addFilter(_PrivateHTTPFilter())

    async def send(self, *args, **kwargs):
        token = _private_http.set(True)
        try:
            return await super().send(*args, **kwargs)
        finally:
            _private_http.reset(token)

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        # 流式读取在 send 返回后仍可能产生含签名 URL 的底层日志。
        token = _private_http.set(True)
        try:
            async with super().stream(*args, **kwargs) as response:
                yield response
        finally:
            _private_http.reset(token)
