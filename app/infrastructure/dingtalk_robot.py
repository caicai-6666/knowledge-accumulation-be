"""机器人异步 OpenAPI；发送失败不盲目重试，临时文件只返回字节，不决定落盘位置。"""
import asyncio
import json
import logging
import re
from time import monotonic
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.config.dingtalk_robot import DingTalkRobotSettings
from app.infrastructure.dingtalk_http import DingTalkHTTPClient
from app.infrastructure.public_dns import resolve_public_address

API_ROOT = 'https://api.dingtalk.com'
ROBOT_TOPIC = '/v1.0/im/bot/messages/get'
class RobotUnavailable(RuntimeError):
    def __init__(self, operation: str, *, status: int | None = None, uncertain: bool = False,
                 reason: str | None = None, download_host: str | None = None,
                 download_scheme: str | None = None):
        super().__init__('钉钉机器人操作失败')
        self.operation = operation
        self.status = status
        self.reason = reason
        self.download_host = download_host
        self.download_scheme = download_scheme
        # 发送超时/异常响应不能证明钉钉没有收到；交付层须保留不确定状态，不自动再次发送。
        self.uncertain = uncertain


class RobotAttachmentInvalid(RobotUnavailable):
    """下载已明确发现空文件或超限，可反馈用户；网络失败不能伪装成文件问题。"""
    def __init__(self):
        super().__init__('download')


class _Token(BaseModel):
    accessToken: SecretStr = Field(min_length=1)
    expireIn: int = Field(strict=True, gt=0)


class RobotSendReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra='ignore', hide_input_in_errors=True)
    processQueryKey: str = Field(strict=True, min_length=1)
    invalidStaffIdList: list[str] = Field(default_factory=list)
    flowControlledStaffIdList: list[str] = Field(default_factory=list)
    filteredStaffIdList: list[str] = Field(default_factory=list)


def _nonempty(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('机器人请求参数不能为空')
    return value


class DingTalkRobotClient:
    def __init__(self, settings: DingTalkRobotSettings, *, transport=None, download_transport=None,
                 resolver=resolve_public_address):
        settings.require_credentials()
        self.settings = settings
        self._http = DingTalkHTTPClient(proxy_url=settings.dingtalk_proxy_url, transport=transport, timeout=settings.timeout_seconds)
        # 文件下载不得复用带鉴权头、Cookie 或平台会话的客户端。
        self._files = DingTalkHTTPClient(proxy_url=settings.dingtalk_proxy_url, transport=download_transport, timeout=settings.timeout_seconds)
        self._resolver = resolver
        self._token = None
        self._expires_at = 0
        self._token_lock = asyncio.Lock()
        self._closed = False

    def _check_open(self):
        if self._closed:
            raise RuntimeError('机器人客户端已关闭')

    async def close(self):
        self._closed = True
        async with self._token_lock:
            self._token = None
            self._expires_at = 0
        try:
            await self._http.aclose()
        finally:
            await self._files.aclose()

    async def _post(self, path, body, *, token=None, operation='api'):
        self._check_open()
        headers = {'x-acs-dingtalk-access-token': token} if token is not None else {}
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._http.stream('POST', API_ROOT + path, json=body, headers=headers) as response:
                    if response.status_code != 200:
                        raise RobotUnavailable(operation, status=response.status_code,
                                               uncertain=operation == 'send' and response.status_code >= 500)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 1048576:
                            raise ValueError('响应超限')
                        data.extend(chunk)
                result = json.loads(data)
                if not isinstance(result, dict) or 'code' in result or result.get('errcode', 0) != 0:
                    raise ValueError('响应不是成功业务对象')
                return result
        except RobotUnavailable:
            raise
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError) as error:
            raise RobotUnavailable(operation, uncertain=operation == 'send', reason=type(error).__name__) from None

    async def _application_token(self):
        self._check_open()
        async with self._token_lock:
            self._check_open()
            if self._token is None or monotonic() >= self._expires_at:
                payload = await self._post('/v1.0/oauth2/accessToken', {
                    'appKey': self.settings.app_key.get_secret_value(),
                    'appSecret': self.settings.app_secret.get_secret_value(),
                }, operation='token')
                try:
                    value = _Token.model_validate(payload)
                    _nonempty(value.accessToken.get_secret_value())
                except (ValueError, TypeError):
                    raise RobotUnavailable('token') from None
                self._token = value.accessToken
                self._expires_at = monotonic() + value.expireIn - min(60, value.expireIn / 10)
            return self._token.get_secret_value()

    async def _authenticated_post(self, path, body, *, operation):
        token = await self._application_token()
        try:
            return await self._post(path, body, token=token, operation=operation)
        except RobotUnavailable as error:
            if error.status == 401:
                async with self._token_lock:
                    if self._token is not None and self._token.get_secret_value() == token:
                        self._token = None
            # 即使认证失效，本次也不自动重发发送请求；下一次调用才重新获取应用凭据。
            raise

    async def send_text(self, userid: str, content: str) -> RobotSendReceipt:
        return await self._send(userid, 'sampleText', {'content': _nonempty(content)})

    async def send_markdown(self, userid: str, title: str, text: str) -> RobotSendReceipt:
        return await self._send(userid, 'sampleMarkdown', {'title': _nonempty(title), 'text': _nonempty(text)})

    async def send_file(self, userid: str, filename: str, content: bytes) -> RobotSendReceipt:
        """上传 PDF 媒体后单独发送文件；媒体上传失败不等于用户消息结果不明。"""
        if (not filename.endswith('.pdf') or not content.startswith(b'%PDF-')
                or len(content) > 20 * 1024 * 1024):
            raise RobotUnavailable('upload', reason='invalid_pdf')
        token = await self._application_token()
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._http.stream(
                    'POST', 'https://oapi.dingtalk.com/media/upload',
                    params={'access_token': token}, data={'type': 'file'},
                    files={'media': (filename, content, 'application/pdf')},
                ) as response:
                    if response.status_code != 200:
                        raise RobotUnavailable('upload', status=response.status_code)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 1048576:
                            raise ValueError('响应超限')
                        data.extend(chunk)
                payload = json.loads(data)
                if not isinstance(payload, dict) or payload.get('errcode') != 0:
                    raise ValueError('媒体上传未成功')
                media_id = _nonempty(payload.get('media_id'))
        except RobotUnavailable as error:
            if error.status == 401:
                async with self._token_lock:
                    if self._token is not None and self._token.get_secret_value() == token:
                        self._token = None
            raise
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError) as error:
            # 此阶段没有调用发送接口，可以安全重试；不回显含凭据的异常原文。
            raise RobotUnavailable('upload', reason=type(error).__name__) from None
        return await self._send(userid, 'sampleFile', {
            'mediaId': media_id, 'fileName': filename, 'fileType': 'pdf',
        })

    async def _send(self, userid, key, params):
        _nonempty(userid)
        if userid != userid.strip() or len(userid) > 128 or any(c.isspace() for c in userid):
            raise ValueError('接收用户标识不合法')
        payload = await self._authenticated_post('/v1.0/robot/oToMessages/batchSend', {
            'robotCode': self.settings.robot_code, 'userIds': [userid],
            'msgKey': key, 'msgParam': json.dumps(params, ensure_ascii=False),
        }, operation='send')
        try:
            receipt = RobotSendReceipt.model_validate(payload)
            _nonempty(receipt.processQueryKey)
        except (ValueError, TypeError):
            raise RobotUnavailable('send', uncertain=True) from None
        rejected = receipt.invalidStaffIdList + receipt.flowControlledStaffIdList + receipt.filteredStaffIdList
        if any(value != userid for value in rejected):
            raise RobotUnavailable('send', uncertain=True)
        if rejected:
            raise RobotUnavailable('recipient')
        return receipt

    async def open_stream(self) -> SecretStr:
        payload = await self._post('/v1.0/gateway/connections/open', {
            'clientId': self.settings.app_key.get_secret_value(),
            'clientSecret': self.settings.app_secret.get_secret_value(),
            'subscriptions': [{'type': 'CALLBACK', 'topic': ROBOT_TOPIC}],
            'ua': 'knowledge-accumulation-python/1.0',
        }, operation='stream')
        try:
            url = httpx.URL(_nonempty(payload.get('endpoint')))
            if (url.scheme != 'wss' or not url.host.endswith('.dingtalk.com') or url.userinfo
                    or url.port not in (None, 443) or url.query or url.fragment):
                raise ValueError('无效长连接端点')
            ticket = _nonempty(payload.get('ticket'))
            return SecretStr(str(url) + '?' + urlencode({'ticket': ticket}))
        except (ValueError, TypeError, httpx.InvalidURL):
            raise RobotUnavailable('stream') from None

    async def download_file(self, download_code: SecretStr) -> bytes:
        """用临时码换链接并下载；不暴露签名 URL，不使用源文件名创建磁盘文件。"""
        if not isinstance(download_code, SecretStr):
            raise ValueError('下载码必须使用 SecretStr')
        code = _nonempty(download_code.get_secret_value())
        payload = await self._authenticated_post('/v1.0/robot/messageFiles/download', {
            'robotCode': self.settings.robot_code, 'downloadCode': code,
        }, operation='download')
        stage = 'download_url'
        try:
            value = payload.get('downloadUrl')
            if not isinstance(value, str) or not value.strip():
                raise RobotUnavailable('download', reason='download_url:missing_url')
            url = httpx.URL(value)
            # 钉钉可能返回 HTTP 的 OSS 签名地址；OSS 标准桶域名支持 HTTPS。
            # 只升级已在允许列表内的标准 OSS 桶域名，不发 HTTP、不跟重定向、不改签名路径和查询。
            if (url.scheme == 'http' and url.port in (None, 80) and url.host
                    and re.fullmatch(r'[a-z0-9][a-z0-9-]*\.oss-[a-z0-9-]+\.aliyuncs\.com', url.host)
                    and any(url.host == host or url.host.endswith('.' + host)
                            for host in self.settings.download_allowed_hosts)):
                url = url.copy_with(scheme='https', port=None)
            failure = None
            if url.scheme != 'https':
                failure = 'scheme'
            elif not url.host:
                failure = 'missing_host'
            elif url.userinfo:
                failure = 'userinfo'
            elif url.fragment:
                failure = 'fragment'
            elif url.port not in (None, 443):
                failure = 'port'
            elif not any(url.host == host or url.host.endswith('.' + host)
                         for host in self.settings.download_allowed_hosts):
                failure = 'host_not_allowed'
            if failure:
                # 只附带域名和常见协议，不保留路径、查询、片段、用户信息或完整下载链接。
                raise RobotUnavailable('download', reason=f'download_url:{failure}',
                    download_host=url.host or None,
                    download_scheme=url.scheme if url.scheme in ('http', 'https') else 'other')
            async with asyncio.timeout(self.settings.timeout_seconds):
                stage = 'download_dns'
                address = await self._resolver(url.host, 443)
                # 与既有公开资源下载一致，连接固定到校验后的公网 IP，保留 TLS 域名校验。
                import ipaddress
                ip = ipaddress.ip_address(address)
                if not ip.is_global or ip.is_multicast:
                    raise ValueError('下载地址不是公网地址')
                stage = 'download_http'
                async with self._files.stream('GET', url.copy_with(host=str(ip)),
                        headers={'Host': url.host, 'Accept-Encoding': 'identity', 'Cookie': ''},
                        extensions={'sni_hostname': url.host}) as response:
                    if response.status_code != 200:
                        raise RobotUnavailable('download', status=response.status_code, reason=f'{stage}:http_status')
                    stage = 'download_body'
                    declared = response.headers.get('content-length')
                    if declared is not None and not declared.isdecimal():
                        raise ValueError('文件大小响应非法')
                    if declared is not None and int(declared) > self.settings.max_download_bytes:
                        raise RobotAttachmentInvalid()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > self.settings.max_download_bytes:
                            raise RobotAttachmentInvalid()
                        content.extend(chunk)
                    if not content:
                        raise RobotAttachmentInvalid()
                    return bytes(content)
        except (ValueError, TypeError, OSError, TimeoutError, httpx.HTTPError, httpx.InvalidURL) as error:
            # 阶段和异常类别可诊断域名策略、DNS、TLS、响应状态及超时；不保存签名链接或异常原文。
            raise RobotUnavailable('download', reason=f'{stage}:{type(error).__name__}') from None
