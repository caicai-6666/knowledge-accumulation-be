"""附件专用代理传输：CONNECT固定公网IP，隧道TLS仍校验原始域名。"""

import httpcore
import httpx
# 与固定httpx版本的流适配保持一致；只复用异常映射，不修改全局httpcore行为。
from httpx._transports.default import map_httpcore_exceptions


class _TLSNameStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, address, hostname, proxy_hostname=None):
        self.stream, self.address, self.hostname = stream, address, hostname
        self.proxy_hostname = proxy_hostname

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self.stream.write(buffer, timeout)

    async def aclose(self):
        await self.stream.aclose()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        if self.proxy_hostname is not None:
            # httpcore也会把请求SNI扩展用于HTTPS代理首层握手，必须恢复代理自身身份。
            server_hostname = self.proxy_hostname
        else:
            if server_hostname != self.address:
                raise httpcore.ConnectError('附件隧道目标与已校验地址不一致')
            # 只调整目标身份；证书链验证和check_hostname保持启用，不改变CONNECT地址。
            server_hostname = self.hostname
        stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        return _TLSNameStream(stream, self.address, self.hostname)


class _TLSNameBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, address, hostname, proxy_hostname):
        self.backend = httpcore.AnyIOBackend()
        self.address, self.hostname, self.proxy_hostname = address, hostname, proxy_hostname

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self.backend.connect_tcp(host, port, timeout, local_address, socket_options)
        return _TLSNameStream(stream, self.address, self.hostname, self.proxy_hostname)

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream, pool):
        self.stream, self.pool = stream, pool

    async def __aiter__(self):
        with map_httpcore_exceptions():
            async for chunk in self.stream:
                yield chunk

    async def aclose(self):
        try:
            with map_httpcore_exceptions():
                await self.stream.aclose()
        finally:
            await self.pool.aclose()


class PinnedProxyTransport(httpx.AsyncBaseTransport):
    def __init__(self, proxy_url):
        self.proxy = httpx.Proxy(proxy_url)

    async def handle_async_request(self, request):
        hostname = request.extensions.get('sni_hostname')
        # 调用者已完成域名、协议和公网DNS校验；此处再约束专用传输的输入。
        import ipaddress
        ip = ipaddress.ip_address(request.url.host)
        if (request.url.scheme != 'https' or request.url.port not in (None, 443)
                or not ip.is_global or ip.is_multicast or not isinstance(hostname, str) or not hostname):
            raise httpx.ConnectError('附件代理请求缺少已校验目标', request=request)
        # 每次下载独立连接池，禁止相同IP的不同域名复用前一次的TLS身份。
        pool = httpcore.AsyncHTTPProxy(
            proxy_url=str(self.proxy.url), proxy_auth=self.proxy.raw_auth,
            proxy_headers=self.proxy.headers.raw, ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            proxy_ssl_context=httpx.create_ssl_context(verify=True, trust_env=False) if self.proxy.url.scheme == 'https' else None,
            network_backend=_TLSNameBackend(str(ip), hostname, self.proxy.url.host if self.proxy.url.scheme == 'https' else None),
            max_connections=1, max_keepalive_connections=0)
        try:
            with map_httpcore_exceptions():
                result = await pool.handle_async_request(httpcore.Request(
                    method=request.method,
                    url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                                     port=request.url.port, target=request.url.raw_path),
                    headers=request.headers.raw, content=request.stream, extensions=request.extensions))
            return httpx.Response(result.status, headers=result.headers,
                stream=_ResponseStream(result.stream, pool), extensions=result.extensions)
        except BaseException:
            await pool.aclose()
            raise
