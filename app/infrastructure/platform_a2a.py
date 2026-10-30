"""中间件平台 A2A HTTP 适配，复用现有会话且不自行登录。"""
import asyncio
import json
import httpx
from pydantic import ValidationError
from app.model.platform_a2a import (A2APublished, A2ABroadcast, A2AClosed,
                                    A2ADirectWindow, A2AAcknowledged)

PREFIX = 'middleware-service/api/platform-a2a/'


class PlatformA2AError(RuntimeError):
    def __init__(self, operation, *, status=None, uncertain=False):
        super().__init__('平台协作操作未完成')
        self.operation, self.status, self.uncertain = operation, status, uncertain


class PlatformA2AClient:
    def __init__(self, base_url, sessions, *, timeout_seconds=10, transport=None):
        url = httpx.URL(base_url)
        if url.scheme not in ('http', 'https') or not url.host or url.userinfo or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('中间件地址必须是 HTTP 根地址')
        self.sessions = sessions
        self.timeout = timeout_seconds
        self.http = httpx.AsyncClient(base_url=str(url).rstrip('/')+'/', transport=transport,
            timeout=timeout_seconds, trust_env=False, follow_redirects=False)

    async def close(self):
        await self.http.aclose()

    async def _call(self, path, body, model, *, operation, expected=200, key=None):
        token = self.sessions.get_access_token()
        if token is None:
            raise PlatformA2AError(operation, status=503)
        headers = {'Authorization': 'Bearer '+token.get_secret_value()}
        if key is not None:
            headers['Idempotency-Key'] = key
        mutating = operation != 'read'
        try:
            async with asyncio.timeout(self.timeout):
                async with self.http.stream('POST', PREFIX+path, json=body, headers=headers) as response:
                    if response.status_code != expected:
                        if response.status_code == 401:
                            self.sessions.invalidate_access_token(token)
                        raise PlatformA2AError(operation, status=response.status_code,
                            uncertain=mutating and (response.status_code >= 500 or 200 <= response.status_code < 300))
                    if response.headers.get('content-type', '').split(';', 1)[0] != 'application/json':
                        raise ValueError('响应不是 JSON')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        # 允许 100 个目标各返回 10000 字符的 UTF-8 正文，同时保留响应体上限。
                        if len(data) + len(chunk) > 8_388_608:
                            raise ValueError('响应超限')
                        data.extend(chunk)
            return model.model_validate(json.loads(data))
        except PlatformA2AError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, ValidationError):
            raise PlatformA2AError(operation, uncertain=mutating) from None

    async def publish(self, request):
        return await self._call('requests/publish', request.model_dump(exclude_none=True), A2APublished,
                                operation='publish', expected=201)

    async def read(self, broadcast_id):
        return await self._call('replies/pull', {'broadcast_id': broadcast_id}, A2ABroadcast, operation='read')

    async def close_broadcast(self, broadcast_id):
        result = await self._call('replies/ack', {'broadcast_id': broadcast_id}, A2AClosed, operation='close')
        if result.broadcast_id != broadcast_id:
            raise PlatformA2AError('close', uncertain=True)
        return result

    async def pull(self, key, renew_messages=()):
        body = {'renew_messages': [{'message_id': d.message.message_id,
                 'delivery_token': d.delivery_token.get_secret_value()} for d in renew_messages]}
        return await self._call('direct-replies/pull', body, A2ADirectWindow, operation='pull', key=key)

    async def acknowledge(self, delivery):
        result = await self._call('direct-replies/ack', {
            'message_id': delivery.message.message_id,
            'delivery_token': delivery.delivery_token.get_secret_value()}, A2AAcknowledged, operation='ack')
        if result.message_id != delivery.message.message_id:
            raise PlatformA2AError('ack', uncertain=True)
        return result
