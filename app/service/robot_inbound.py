"""整条机器人消息校验、可靠暂存及入队；不执行模型，不创建用户登录或权限。"""
import asyncio
from datetime import timedelta
import hashlib
import json
import logging
from uuid import NAMESPACE_URL, uuid5

from app.infrastructure.assistant_queue import AssistantQueueError
from app.infrastructure.dingtalk_robot import RobotUnavailable, RobotAttachmentInvalid
from app.infrastructure.robot_attachment import AttachmentRejected, supported, to_pdf, validate_pdf
from app.model.assistant_queue import AssistantUpload, PublishAssistantTask
from app.model.question_file import UploadedFile
from app.model.question_tree import utc_now
from app.model.robot_inbox import RobotInboxFile, RobotInboxRecord

logger = logging.getLogger(__name__)


def _fingerprint(message):
    # 下载码会随钉钉重推变化，且不能落盘；业务身份与正文仍必须精确相同。
    value = message.model_dump(mode='json')
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _label(part, index):
    text = part.filename or {'picture': '图片', 'audio': '音频', 'video': '视频'}.get(part.type, '附件')
    return f'附件 {index + 1}（{" ".join(text.split())[:120]}）'


async def _finish_io(awaitable):
    # 文件线程不随协程取消停止；等待写入/删除结束后才能释放消息锁。
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class RobotInboundService:
    def __init__(self, settings, repository, employees, queue, robot, files, *, clock=utc_now):
        self.settings, self.repository, self.employees = settings, repository, employees
        self.queue, self.robot, self.files, self.clock = queue, robot, files, clock
        self._task = None
        self._closed = False
        self._active = set()
        self._tick_lock = asyncio.Lock()

    async def _update(self, record, **changes):
        return await self.repository.update(record.request_id, updated_at=self.clock(), **changes)

    async def _reject(self, record, text):
        return await self._update(record, status='reply_pending', reply_text=text,
                                  next_attempt_at=self.clock())

    async def _prepare(self, record, message):
        parts = [part for part in message.parts if part.type != 'text']
        # 先检查整个列表，再下载任何附件；不能把剩余文字或合法文件单独入队。
        for index, part in enumerate(parts):
            if not supported(part):
                return await self._reject(record, f'{_label(part,index)}：暂不支持该文件类型。'
                    '本条消息未接收，请调整后重新发送整条消息。')
        limits = self.queue.client.settings
        if len(parts) > limits.max_files:
            return await self._reject(record, '本条消息附件数量超出限制，请减少附件后重新发送整条消息。')
        if await self.employees.find_by_userid(message.userid) is None:
            return await self._reject(record, '账号资料尚未准备好，请先登录知识平台，再重新发送整条消息。')
        prepared = []
        for index, (part, entry) in enumerate(zip(parts, record.files, strict=True)):
            try:
                try:
                    content = await self.files.read(entry.reference)
                    await asyncio.to_thread(validate_pdf, content)
                except FileNotFoundError:
                    original = await self.robot.download_file(part.download_code)
                    content = await asyncio.to_thread(to_pdf, part, original, self.robot.settings,
                        max_bytes=min(limits.max_file_bytes, self.files.max_bytes))
                    await _finish_io(self.files.save_reference(
                        UploadedFile(type='images', content=content), entry.reference))
                if len(content) > limits.max_file_bytes:
                    raise AttachmentRejected('文件大小超出限制')
            except (AttachmentRejected, RobotAttachmentInvalid) as error:
                reason = '文件为空或超过下载大小限制' if isinstance(error, RobotAttachmentInvalid) else str(error)
                return await self._reject(record, f'{_label(part,index)}：{reason}。'
                    '本条消息未接收，请调整后重新发送整条消息。')
            prepared.append(RobotInboxFile(file_id=entry.file_id, sha256=hashlib.sha256(content).hexdigest(),
                                          size=len(content)))
        return await self._update(record, files=tuple(prepared), status='publish_pending',
                                  next_attempt_at=self.clock())

    async def receive(self, message):
        if self._closed or not self.settings.enabled:
            return False
        current = asyncio.current_task()
        self._active.add(current)
        try:
            async with self.repository.scope(message.request_id) as acquired:
                if not acquired or self._closed:
                    return False
                record = await self.repository.get(message.request_id)
                fingerprint = _fingerprint(message)
                if record is not None and record.fingerprint != fingerprint:
                    raise ValueError('重复消息的业务内容不一致')
                if record is None:
                    text = '\n'.join(part.text for part in message.parts if part.type == 'text').strip()
                    now = self.clock()
                    record = RobotInboxRecord(request_id=message.request_id, userid=message.userid,
                        fingerprint=fingerprint, message=text[:10000] or '请查看附件。',
                        files=tuple(RobotInboxFile(file_id=str(uuid5(NAMESPACE_URL,
                            f'knowledge-robot-inbound:{message.request_id}:{index}')))
                            for index, part in enumerate(message.parts) if part.type != 'text'),
                        next_attempt_at=now, created_at=now, updated_at=now)
                    record = await self.repository.create(record)
                    if len(text) > 10000:
                        record = await self._reject(record, '本条消息文字超出长度限制，请调整后重新发送整条消息。')
                if record.status == 'received':
                    text = '\n'.join(part.text for part in message.parts if part.type == 'text').strip()
                    if len(text) > 10000:
                        record = await self._reject(record, '本条消息文字超出长度限制，请调整后重新发送整条消息。')
                    else:
                        record = await self._prepare(record, message)
                # 到这里正文和全部附件已可靠暂存，或拒绝通知已落盘；不等待模型/中央可用。
                # 交给同一后台 worker，避免中央请求耗尽 Stream 确认时限。
                return record.status != 'received'
        finally:
            self._active.discard(current)

    async def _process(self, request_id):
        async with self.repository.scope(request_id) as acquired:
            if not acquired or self._closed:
                return
            record = await self.repository.get(request_id)
            now = self.clock()
            if record is None or record.next_attempt_at > now:
                return
            if record.status in ('publish_pending', 'publish_unknown'):
                if (record.publish_started_at is not None and now >= record.publish_started_at
                        + timedelta(seconds=self.settings.publication_retry_window_seconds)):
                    await self._update(record, status='publish_blocked')
                    return
                uploads = []
                for entry in record.files:
                    content = await self.files.read(entry.reference)
                    if len(content) != entry.size or hashlib.sha256(content).hexdigest() != entry.sha256:
                        raise ValueError('入站附件校验失败')
                    uploads.append(AssistantUpload(file_extension='pdf', content=content))
                record = await self._update(record, status='publish_unknown',
                    publish_started_at=record.publish_started_at or now,
                    next_attempt_at=now + timedelta(seconds=self.settings.inbound_retry_seconds))
                try:
                    await self.queue.publish(PublishAssistantTask(userid=record.userid, task_type='user_message',
                        message=record.message, request_id=record.request_id), uploads=tuple(uploads))
                except AssistantQueueError as error:
                    if not error.uncertain:
                        await self._update(record, status='publish_blocked' if error.status == 409 else 'publish_pending')
                    raise
                record = await self._update(record, status='published')
            elif record.status == 'reply_pending':
                # 钉钉发送没有客户端幂等键；结果不确定时保留待核对状态，不能盲重发通知。
                record = await self._update(record, status='reply_unknown',
                    next_attempt_at=now + timedelta(seconds=self.settings.inbound_retry_seconds))
                try:
                    await self.robot.send_text(record.userid, record.reply_text)
                except RobotUnavailable as error:
                    if not error.uncertain:
                        await self._update(record, status='reply_pending')
                    raise
                record = await self._update(record, status='rejected')
            if record.status in ('published', 'rejected'):
                for entry in record.files:
                    await _finish_io(self.files.delete(entry.reference))
                if record.files:
                    await self._update(record, files=())

    async def tick(self):
        if self._closed or not self.settings.enabled:
            return
        async with self._tick_lock:
            ids = await self.repository.pending(now=self.clock(), retry_cutoff=self.clock()
                - timedelta(seconds=self.settings.publication_retry_window_seconds),
                limit=self.settings.inbound_batch_size)
            for request_id in ids:
                try:
                    # 跨实例消息锁丢失时只取消本次处理，下一次扫描仍能继续。
                    await asyncio.create_task(self._process(request_id))
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    logger.warning('机器人入站处理已取消，等待后续检查')
                except Exception as error:
                    logger.warning('机器人入站处理未完成：error_type=%s', type(error).__name__)

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except Exception as error:
                logger.warning('机器人入站扫描失败：error_type=%s', type(error).__name__)
            await asyncio.sleep(self.settings.inbound_poll_seconds)

    async def start(self):
        if self._closed:
            raise RuntimeError('机器人接收器已关闭')
        if self.settings.enabled and self._task is None:
            await self.repository.validate_schema()
            self._task = asyncio.create_task(self._run())

    async def close(self):
        self._closed = True
        tasks = set(self._active)
        if self._task is not None:
            tasks.add(self._task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
