"""整条机器人消息校验、首次员工初始化、可靠暂存及入队；不执行模型或创建用户登录。"""
import asyncio
from datetime import timedelta
import hashlib
import json
import logging
from textwrap import dedent
from uuid import NAMESPACE_URL, uuid5

from app.infrastructure.assistant_queue import AssistantQueueError
from app.infrastructure.dingtalk_robot import RobotUnavailable, RobotAttachmentInvalid
from app.infrastructure.robot_attachment import AttachmentRejected, supported, to_pdf, validate_pdf
from app.model.assistant_queue import AssistantUpload, PublishAssistantTask
from app.model.question_file import UploadedFile, AttachmentReference
from app.agent.core.context_rendering.attachment import render_attachments
from app.model.question_tree import utc_now
from app.model.robot_inbox import RobotInboxFile, RobotInboxRecord
from app.service.employee_onboarding import EmployeeOnboardingService

logger = logging.getLogger(__name__)


def _fingerprint(message):
    # 下载码会随钉钉重推变化，且不能落盘；业务身份与正文仍必须精确相同。
    value = message.model_dump(mode='json')
    if value.get('reply') is None:
        value.pop('reply', None)  # 普通消息沿用旧指纹，升级后重推仍可去重。
    elif value['reply'] is not None:
        reply = value['reply']
        if reply.get('kind') in (None, 'text', 'picture'):
            reply.pop('kind', None)
        for name in ('from_assistant', 'needs_local'):
            if not reply.get(name):
                reply.pop(name, None)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def render_inbound_question(message, *, reused_file_id=None, resolved_richtext=None):
    current = '\n'.join(part.text for part in message.parts if part.type == 'text').strip()
    reply = message.reply
    if reply is None:
        return current or '请查看附件。'
    sections = []
    if resolved_richtext is not None:
        text, references = resolved_richtext
        if text:
            sections.append('\n'.join('> ' + line for line in text.splitlines()))
        if references:
            sections.append(render_attachments(list(references)))
    elif reused_file_id is not None:
        sections.append(render_attachments([AttachmentReference(type='images', file_id=reused_file_id)]))
    else:
        # 连续文字是同一段；图片前后分别分区，保留回调中能确定的片段顺序。
        text = ''
        file_number = 0
        for part in reply.parts:
            if part.type == 'text':
                text += part.text
                continue
            if text:
                sections.append('\n'.join('> ' + line for line in text.splitlines()))
                text = ''
            file_number += 1
            sections.append(f'本条消息的第 {file_number} 个附件。')
        if text:
            sections.append('\n'.join('> ' + line for line in text.splitlines()))
    if not sections:
        return current or '请查看引用内容。'  # 未恢复的引用随后整条拒绝。
    title = '引用对象：助理此前的回复' if reply.from_assistant else '引用对象'
    return dedent("""
        【{title}】
        {original}

        【当前消息】
        {current}
    """).strip().format(title=title, original='\n\n'.join(sections),
                         current=current or '请查看本次附带的内容。')


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
    def __init__(self, settings, repository, employees, queue, robot, files, *, clock=utc_now,
                 onboarding: EmployeeOnboardingService | None = None, reply_files=None):
        self.settings, self.repository, self.employees = settings, repository, employees
        self.queue, self.robot, self.files, self.clock = queue, robot, files, clock
        self.onboarding = onboarding
        self.reply_files = reply_files
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
        if message.reply is not None and message.reply.rejection is not None:
            return await self._reject(record, message.reply.rejection)
        resolved_richtext = None
        if message.reply is not None and message.reply.kind == 'richText':
            if self.reply_files is not None:
                resolved_richtext = await self.reply_files.resolve_richtext(message)
            if message.reply.needs_local and resolved_richtext is None:
                return await self._reject(record,
                    '钉钉未提供被引用图文的真实内容，本地也未找到完整可用的原消息。本条消息未提交给助理，请直接重发原文字和图片，并附上本次问题。')
        parts = [part for part in message.task_parts if part.type != 'text']
        # 先检查整个列表，再下载任何附件；不能把剩余文字或合法文件单独入队。
        for index, part in enumerate(parts):
            if part.type == 'unsupported':
                return await self._reject(record, '这条消息的类型暂不支持，无法读取内容。'
                    '请改用文字、图片或 PDF 后重新发送整条消息。')
            if not supported(part):
                return await self._reject(record, f'{_label(part,index)}：暂不支持该文件类型。'
                    '本条消息未接收，请调整后重新发送整条消息。')
        limits = self.queue.client.settings
        if len(parts) > limits.max_files:
            return await self._reject(record, '本条消息附件数量超出限制，请减少附件后重新发送整条消息。')
        if await self.employees.find_by_userid(message.userid) is None:
            if self.onboarding is None:
                raise RuntimeError('机器人首次员工初始化服务未装配')
            # userid 只来自已验证企业和机器人的 Stream；复用网页登录的初始化锁及事务。
            # 失败保留 received，不下载附件、不发布任务、不确认接收，允许原消息重推恢复。
            user = await self.onboarding.initialize(message.userid)
            if user.userid != message.userid:
                raise ValueError('初始化员工与机器人发送者不一致')
        entries = record.files
        reused = None
        if resolved_richtext is not None:
            count = sum(part.type != 'text' for part in message.reply.parts)
            parts, entries = parts[count:], entries[count:]
        elif self.reply_files is not None and message.reply is not None and message.reply.kind != 'richText':
            reused = await self.reply_files.resolve(message)
            if reused is not None:
                # 引用图始终是首个附件；从暂存清单排除，防止上传或清理已有的用户文件。
                parts, entries = parts[1:], entries[1:]
        text = render_inbound_question(message, reused_file_id=reused.file_id if reused else None,
                                       resolved_richtext=resolved_richtext)
        if len(text) > 10000:
            return await self._reject(record, '引用内容与本次消息合计超过长度限制，本条消息未提交给助理，请缩短引用范围或拆分问题后发送。')
        prepared = []
        for index, (part, entry) in enumerate(zip(parts, entries, strict=True)):
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
                                  message=text,
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
                # 只串行登记，不持有用户锁下载/转换；图片一登记就占住原位置。
                async with self.repository.user_scope(message.userid) as admitted:
                    if not admitted:
                        return False
                    record = await self.repository.get(message.request_id)
                    fingerprint = _fingerprint(message)
                    if record is not None and record.fingerprint != fingerprint:
                        raise ValueError('重复消息的业务内容不一致')
                    if record is None:
                        text = render_inbound_question(message)
                        now = self.clock()
                        record = RobotInboxRecord(request_id=message.request_id, userid=message.userid,
                            fingerprint=fingerprint, message=text[:10000] or '请查看附件。',
                            files=tuple(RobotInboxFile(file_id=str(uuid5(NAMESPACE_URL,
                                f'knowledge-robot-inbound:{message.request_id}:{index}')))
                                for index, part in enumerate(message.task_parts) if part.type != 'text'),
                            next_attempt_at=now, created_at=now, updated_at=now)
                        record = await self.repository.create(record)
                        if len(text) > 10000:
                            record = await self._reject(record, '本条消息文字超出长度限制，请调整后重新发送整条消息。')
                # 人工终止保留去重记录；迟到重推仅确认，不再次下载、发布或发送通知。
                if record.status == 'cancelled':
                    return True
                if record.status == 'received':
                    text = render_inbound_question(message)
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
            if record is None:
                return
            async with self.repository.user_scope(record.userid) as admitted:
                if not admitted:
                    return
                await self._process_ordered(record)

    async def _process_ordered(self, record):
        # 消息锁与用户发布锁同时持有；网络调用不占用 SQL 事务。
        now = self.clock()
        if record is None or record.next_attempt_at > now:
            return
        if record.status in ('publish_pending', 'publish_unknown'):
            # 扫描之后仍需复核；慢图片、重试退避及结果未知都不能让后续文字超车。
            # 前序只有确认发布或明确拒绝后才放行，不能因超时静默跳过。
            if await self.repository.has_unpublished_predecessor(record):
                return
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
