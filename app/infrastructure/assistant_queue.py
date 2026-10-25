"""中间件助理队列异步传输；不登录平台、不维护第二个队列、不自动重试写请求。"""
import asyncio
import json

import httpx
from pydantic import SecretStr, TypeAdapter

from app.config.assistant_queue import AssistantQueueSettings
from app.model.assistant import QueueFileReference, UserID
from app.model.assistant_queue import (AssistantAcknowledged, AssistantDeliveryLease, AssistantPublished,
    AssistantQueueStatus, AssistantUpload, PublishAssistantTask)

PREFIX = 'middleware-service/api/assistant/tasks/'
_EMPTY = object()


class AssistantQueueError(RuntimeError):
    def __init__(self, operation, *, status=None, uncertain=False):
        super().__init__('助理队列操作未完成')
        self.operation = operation
        self.status = status
        self.uncertain = uncertain


class AssistantQueueClient:
    def __init__(self, base_url: str, *, settings: AssistantQueueSettings | None = None, transport=None):
        url = httpx.URL(base_url)
        if (url.scheme not in ('http', 'https') or not url.host or url.userinfo
                or url.path not in ('', '/') or url.query or url.fragment):
            raise ValueError('中间件地址必须是不带凭据或路径的 HTTP 根地址')
        self.settings = settings or AssistantQueueSettings()
        self._http = httpx.AsyncClient(base_url=str(url).rstrip('/')+'/', timeout=self.settings.timeout_seconds,
            trust_env=False, follow_redirects=False, transport=transport)

    async def close(self):
        await self._http.aclose()

    async def _request(self, method, path, token, *, operation, expected=200, binary=False, **kwargs):
        if not isinstance(token, SecretStr) or not token.get_secret_value().strip():
            raise ValueError('队列调用需要有效平台凭据')
        mutating = operation in ('publish', 'pull', 'ack')
        headers = {'Authorization': 'Bearer '+token.get_secret_value()}
        if binary:
            # 按原始文件长度校验完整性，避免压缩传输长度与解压后的字节数混用。
            headers['Accept-Encoding'] = 'identity'
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._http.stream(method, path,
                    headers=headers, **kwargs) as response:
                    status = response.status_code
                    if operation == 'pull' and status == 204:
                        # 204 仅代表无任务，其他失败绝不能降级为 None。
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                raise ValueError('204 响应不得包含正文')
                        return _EMPTY
                    if status != expected:
                        raise AssistantQueueError(operation, status=status,
                            uncertain=mutating and (status >= 500 or 200 <= status < 300))
                    limit = self.settings.max_file_bytes if binary else self.settings.max_response_bytes
                    length = response.headers.get('content-length')
                    if length is not None and (not length.isdecimal() or int(length) > limit):
                        raise ValueError('响应大小不合法')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data)+len(chunk) > limit:
                            raise ValueError('响应超限')
                        data.extend(chunk)
                    if binary:
                        if not data or length is not None and len(data) != int(length):
                            raise ValueError('附件下载不完整')
                        return bytes(data)
                    if response.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json':
                        raise ValueError('响应不是 JSON')
                    return json.loads(data)
        except AssistantQueueError:
            raise
        except (ValueError, TypeError, httpx.HTTPError, TimeoutError):
            raise AssistantQueueError(operation, uncertain=mutating) from None

    async def publish(self, request: PublishAssistantTask, token: SecretStr, *, platform_code: str, uploads=()):
        request = PublishAssistantTask.model_validate(request.model_dump())
        uploads = tuple(AssistantUpload(file_extension=item.file_extension, content=item.content) for item in uploads)
        if len(uploads) > self.settings.max_files or any(len(item.content)>self.settings.max_file_bytes for item in uploads):
            raise ValueError('队列附件数量或大小超限')
        if uploads:
            # 重复字段按两组各自顺序对应；文件名不承载业务含义，不读取调用者本地路径。
            parts = [(key, (None, value)) for key, value in request.model_dump(exclude_none=True).items()]
            for index, upload in enumerate(uploads):
                parts.extend([('file_extensions',(None,upload.file_extension)),
                    ('files',(f'attachment-{index}.{upload.file_extension}',upload.content,'application/octet-stream'))])
            payload = await self._request('POST', PREFIX+'publish', token, operation='publish', expected=201, files=parts)
        else:
            payload = await self._request('POST', PREFIX+'publish', token, operation='publish', expected=201,
                                          json=request.model_dump(exclude_none=True))
        try:
            result = AssistantPublished.model_validate(payload)
            task = result.task
            if task.userid != request.userid or task.task_type != request.task_type:
                raise ValueError('发布响应身份不一致')
            if task.task_type == 'user_message':
                matches = [item for item in task.user_messages
                    if item.source_platform_code == platform_code and item.request_id == request.request_id]
                if len(matches) != 1 or result.merged != (len(task.user_messages)>1):
                    raise ValueError('合并任务不包含唯一的本次消息')
                item = matches[0]
            else:
                item = task
                if result.merged:
                    raise ValueError('仅用户消息可以合并')
            if (item.source_platform_code != platform_code or item.request_id != request.request_id
                    or item.source_assistant_id != request.source_assistant_id
                    or item.message != request.message
                    or [file.file_extension for file in item.files] != [upload.file_extension for upload in uploads]):
                raise ValueError('发布响应与本次内容不一致')
            return result
        except (ValueError, TypeError):
            raise AssistantQueueError('publish', uncertain=True) from None

    async def pull(self, userid, token):
        userid = TypeAdapter(UserID).validate_python(userid)
        payload = await self._request('POST',PREFIX+'pull',token,operation='pull',json={'userid':userid})
        if payload is _EMPTY:
            return None
        try:
            result = AssistantDeliveryLease.model_validate(payload)
            if result.task.userid != userid:
                raise ValueError('领取任务用户不一致')
            return result
        except (ValueError, TypeError):
            raise AssistantQueueError('pull', uncertain=True) from None

    async def acknowledge(self, lease: AssistantDeliveryLease, token):
        payload = await self._request('POST',PREFIX+'ack',token,operation='ack',json={
            'userid':lease.task.userid,'task_id':lease.task.task_id,
            'delivery_token':lease.delivery_token.get_secret_value()})
        try:
            result = AssistantAcknowledged.model_validate(payload)
            if result.task_id != lease.task.task_id:
                raise ValueError('确认任务不一致')
            return result
        except (ValueError, TypeError):
            raise AssistantQueueError('ack', uncertain=True) from None

    async def status(self, userid, token):
        userid = TypeAdapter(UserID).validate_python(userid)
        payload = await self._request('GET',PREFIX+'status',token,operation='status',params={'userid':userid})
        try:
            result = AssistantQueueStatus.model_validate(payload)
            if result.userid != userid:
                raise ValueError('状态用户不一致')
            return result
        except (ValueError, TypeError):
            raise AssistantQueueError('status') from None

    async def download(self, reference: QueueFileReference, token):
        reference = QueueFileReference.model_validate(reference.model_dump())
        return await self._request('GET','middleware-service/api/resource/assistant-tasks/'+reference.file_id,
            token,operation='download',binary=True,params={'file_extension':reference.file_extension})
