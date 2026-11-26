"""基于 agent-core TreeAgent 的探索任务与 SSE 服务。

保持前端契约并直接复用 agent-core 原生能力：
- `register`: `deferred=True` 提交给 `TreeAgent.generate(..., parent_id=..., advance_head=(mode == 'main'))` 或
  `TreeAgent.redirect(..., deferred=True)`
- `subscribe`: 首次订阅调 `await job.activate()`，后台消费 `job.events()` 并按 `AgentEvent.sequence` 对齐序号，
  映射为前端 SSE 白名单事件，缓冲最近 512 条事件支持断线重连（断开 SSE 不取消后台执行）
- `snapshot`: 返回 `TurnSnapshot`
- `cancel`: 调 `await agent.stop(turn_id)` 并返回终态 `TurnSnapshot`
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import re
import time
from typing import Any

from agent_core import SessionTask, StorageConflict, TreeAgent, TreeView
import pymupdf
from starlette.concurrency import run_in_threadpool

from app.auth.model import AuthenticatedUser
from app.infrastructure.pdf_lock import serialized_pdf_operation
from app.infrastructure.upload_file import UploadFileStore
from app.model.question_file import AttachmentReference, UploadedFile
from app.model.question_tree import QuestionTreeConflict, QuestionTreeNotFound, new_tree_id, utc_now
from app.repository.exploration_tree import ExplorationTreeRepository
from app.schema.exploration_turn import TurnCreated, TurnInput, TurnMessage, TurnSnapshot
from app.service.exploration_attachment import SourceUpload
from app.service.exploration_tree import normalize_progress_payload

logger = logging.getLogger(__name__)

TERMINAL = frozenset({'completed', 'cancelled', 'superseded', 'failed', 'expired'})
_TREE_ID_RE = re.compile(r'^[a-f0-9]{32}$')


class TurnExpired(ValueError):
    pass


class ReplayUnavailable(ValueError):
    pass


class InvalidTurnInput(ValueError):
    pass


class TurnCapacity(RuntimeError):
    pass


def context_status(status: str) -> str | None:
    return {'cancelled': 'user_manually_stopped', 'superseded': 'user_goal_adjusted'}.get(status)


@serialized_pdf_operation
def validate_pdfs(uploads: Sequence[UploadedFile]) -> None:
    """校验实际 PDF 内容；文件后缀和浏览器 MIME 不能替代内容校验。"""
    for upload in uploads:
        try:
            with pymupdf.open(stream=upload.content, filetype='pdf') as document:
                if not document.is_pdf or document.needs_pass or document.page_count < 1:
                    raise ValueError('PDF 不可读取')
        except Exception:
            raise ValueError('PDF 损坏、加密或没有页面') from None


def _format_platform_permissions_supplemental(user: AuthenticatedUser) -> str:
    if not user.platform_permissions:
        return ''
    lines = ['### 平台访问等级']
    for code, level in sorted(user.platform_permissions.items()):
        lines.append(f'- {code}: 等级 {level}')
    return '\n'.join(lines)


@dataclass
class _Turn:
    user: AuthenticatedUser
    tree_id: str
    id: str
    request: TurnInput | None
    agent: TreeAgent
    job: SessionTask
    expires: datetime
    deadline: float
    status: str = 'pending_activation'
    activated: datetime | None = None
    finished: datetime | None = None
    started: float | None = None
    duration: int | None = None
    touched: float = field(default_factory=time.monotonic)
    sequence: int = 0
    events: deque = field(default_factory=deque)
    messages: list[TurnMessage] = field(default_factory=list)
    error: dict[str, Any] | None = None
    node_id: str | None = None
    summary_id: str | None = None
    worker: asyncio.Task | None = None
    supersedes_turn_id: str | None = None
    superseded_by_turn_id: str | None = None
    progress: dict[str, str] | None = None
    parent_id: str | None = 'latest'
    mode: str = 'main'


class ExplorationTurnService:
    """探索任务注册、激活、SSE 订阅、快照与取消服务。"""

    def __init__(
        self,
        repository: ExplorationTreeRepository,
        tree_service: Any,
        runtime: Any,
        *,
        upload_files: UploadFileStore | None = None,
        attachment_converter: Callable[[Sequence[SourceUpload]], Awaitable[tuple[UploadedFile, ...]]] | None = None,
        activation_seconds: int = 180,
        retention_seconds: int = 1800,
        heartbeat_seconds: float = 15.0,
        event_limit: int = 512,
        max_turns: int = 128,
    ) -> None:
        self._repository = repository
        self.tree_service = tree_service
        self._runtime = runtime
        self._files = upload_files if upload_files is not None else UploadFileStore()
        self._attachment_converter = attachment_converter
        self.activation_seconds = activation_seconds
        self.retention_seconds = retention_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.event_limit = event_limit
        self.max_turns = max_turns
        self._turns: dict[tuple[str, str, str], _Turn] = {}
        self._condition = asyncio.Condition()
        self._closed = False
        self._reaper: asyncio.Task | None = None

    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._cleanup_loop())

    @staticmethod
    def _project_messages(turn: _Turn, event: str, data: dict[str, Any]) -> list[TurnMessage]:
        messages = list(turn.messages)
        if event not in ('message.delta', 'message.completed'):
            return messages
        index = next((i for i, m in enumerate(messages) if m.message_id == data['message_id']), None)
        previous = messages[index] if index is not None else None
        if event == 'message.delta':
            if previous is not None and previous.status != 'streaming':
                return messages
            message = TurnMessage(
                message_id=data['message_id'],
                message_kind=data['message_kind'],
                text=(previous.text if previous else '') + data['delta'],
                status='streaming',
            )
        else:
            message = TurnMessage(
                message_id=data['message_id'],
                message_kind=data['message_kind'],
                text=data['text'],
                status=data.get('status', 'completed'),
            )
        if index is None:
            messages.append(message)
        else:
            messages[index] = message
        return messages

    def _emit_locked(
        self,
        turn: _Turn,
        event: str,
        data: dict[str, Any],
        *,
        sequence: int | None = None,
    ) -> None:
        seq = max(turn.sequence + 1, sequence or 0)
        item = {'sequence': seq, 'event': event, 'data': {'turn_id': turn.id, **data}}
        turn.messages = self._project_messages(turn, event, item['data'])
        turn.sequence = seq
        turn.events.append(item)
        while len(turn.events) > self.event_limit:
            turn.events.popleft()
        if event == 'task.progress':
            turn.progress = {'type': item['data']['type'], 'message': item['data']['message']}
        turn.touched = time.monotonic()
        self._condition.notify_all()

    def _set_status_locked(
        self,
        turn: _Turn,
        status: str,
        *,
        sequence: int | None = None,
        activated: datetime | None = None,
        finished: datetime | None = None,
        duration: int | None = None,
    ) -> None:
        if status in TERMINAL:
            for message in tuple(turn.messages):
                if message.status == 'streaming':
                    self._emit_locked(
                        turn,
                        'message.completed',
                        {
                            'message_id': message.message_id,
                            'message_kind': message.message_kind,
                            'text': message.text,
                            'status': 'interrupted',
                        },
                    )
        turn.status = status
        if activated is not None:
            turn.activated = activated
        if status in TERMINAL:
            turn.finished = finished if finished is not None else utc_now()
            turn.duration = (
                duration
                if duration is not None
                else (int((time.monotonic() - turn.started) * 1000) if turn.started else 0)
            )
            turn.request = None
        self._emit_locked(
            turn,
            'turn.status',
            {
                'status': status,
                'can_interrupt': status not in TERMINAL,
                'context_status': context_status(status),
                'supersedes_turn_id': turn.supersedes_turn_id,
                'superseded_by_turn_id': turn.superseded_by_turn_id,
                'created_at': (turn.finished if status in TERMINAL and turn.finished else utc_now()).isoformat(),
                'activated_at': turn.activated.isoformat() if turn.activated else None,
                'finished_at': turn.finished.isoformat() if turn.finished else None,
                'processing_duration_ms': turn.duration,
                'node_id': turn.node_id,
                'summary_id': turn.summary_id,
            },
            sequence=sequence,
        )

    def _cleanup_locked(self) -> list[_Turn]:
        now = time.monotonic()
        expired: list[_Turn] = []
        for key, turn in list(self._turns.items()):
            if turn.status == 'pending_activation' and now >= turn.deadline:
                self._set_status_locked(turn, 'expired')
                expired.append(turn)
            if turn.status in TERMINAL and now - turn.touched >= self.retention_seconds:
                del self._turns[key]
        return expired

    async def _stop_expired(self, expired: Sequence[_Turn]) -> None:
        for turn in expired:
            try:
                await turn.agent.stop(turn.id)
            except Exception:
                pass

    def _get_locked(self, tree_id: str, turn_id: str, owner: str) -> _Turn:
        turn = self._turns.get((owner, tree_id, turn_id))
        if turn is None or self._closed:
            raise QuestionTreeNotFound('问答任务不存在')
        return turn

    def _build_supplemental(self, user: AuthenticatedUser) -> str:
        builder = getattr(self._runtime, 'build_supplemental', None)
        if callable(builder):
            return builder(user)
        return _format_platform_permissions_supplemental(user)

    async def _prepare_and_store_uploads(
        self,
        tree_id: str,
        owner_userid: str,
        uploads: Sequence[SourceUpload | UploadedFile],
    ) -> tuple[list[dict[str, str]], list[AttachmentReference]]:
        if not uploads:
            return [], []
        # 按原顺序记录每份附件的原始文件名，避免混传 SourceUpload 与 UploadedFile 时错位。
        filenames: list[str] = []
        source_indices: list[int] = []
        source_uploads: list[SourceUpload] = []
        ordered_uploads: list[UploadedFile | None] = []
        for idx, item in enumerate(uploads):
            if isinstance(item, SourceUpload):
                source_indices.append(idx)
                source_uploads.append(item)
                filenames.append(item.filename)
                ordered_uploads.append(None)
            elif isinstance(item, UploadedFile):
                filenames.append('attachment.pdf')
                ordered_uploads.append(item)
            else:
                raise InvalidTurnInput('不支持的附件对象类型')
        if source_uploads:
            if self._attachment_converter is None:
                raise RuntimeError('未配置附件转换服务')
            converted_sources = await self._attachment_converter(source_uploads)
            for idx, converted_file in zip(source_indices, converted_sources):
                ordered_uploads[idx] = converted_file
        converted = [item for item in ordered_uploads if item is not None]
        try:
            await run_in_threadpool(validate_pdfs, converted)
        except ValueError:
            raise InvalidTurnInput('PDF 损坏、加密或没有页面') from None

        saved_refs: list[AttachmentReference] = []
        registered_rows: list[tuple[str, str]] = []
        try:
            for upload, filename in zip(converted, filenames):
                ref = await self._files.save(upload)
                saved_refs.append(ref)
                registered_rows.append((ref.file_id, filename))
            await self._repository.register_files(tree_id, owner_userid, registered_rows)
        except BaseException:
            for ref in saved_refs:
                try:
                    await self._files.delete(ref)
                except Exception:
                    pass
            raise
        attachments_payload = [{'type': ref.type, 'file_id': ref.file_id} for ref in saved_refs]
        return attachments_payload, saved_refs

    @staticmethod
    def _validate_selection(view: TreeView | None, request: TurnInput) -> str | None:
        nodes = view.nodes if view is not None else []
        if not nodes:
            if request.selected_node_id is not None or request.mode != 'main':
                raise QuestionTreeConflict('空树首轮只能使用推进模式，建立首个主干节点')
            return 'latest'
        if view is not None and view.active is not None:
            raise QuestionTreeConflict('本树已有执行中的任务')
        nodes_by_id = {n.id: n for n in nodes}
        if request.selected_node_id is None or request.selected_node_id not in nodes_by_id:
            raise QuestionTreeConflict('请选择本树任务节点')
        if request.mode == 'main':
            if request.selected_node_id != view.head:
                raise QuestionTreeConflict('推进模式只能延续当前主干末尾；历史主干或分支节点只能使用探索模式')
            return 'latest'
        return request.selected_node_id

    async def register(
        self,
        tree_id: str,
        user: AuthenticatedUser,
        request: TurnInput,
        uploads: Sequence[SourceUpload | UploadedFile] = (),
    ) -> TurnCreated:
        request = TurnInput.model_validate(request.model_dump())
        async with self.tree_service.operation(tree_id, user.userid):
            await self._repository.get_tree(tree_id, user.userid)
            async with self._condition:
                expired = self._cleanup_locked()
            await self._stop_expired(expired)

            if request.supersedes_turn_id is not None:
                return await self._register_redirect(tree_id, user, request, uploads)

            view = await self._runtime.try_tree_view(tree_id, include_display=False)
            parent_id = self._validate_selection(view, request)

            async with self._condition:
                if self._closed:
                    raise RuntimeError('问答任务服务已关闭')
                if any(
                    t.tree_id == tree_id and t.user.userid == user.userid and t.status not in TERMINAL
                    for t in self._turns.values()
                ):
                    raise QuestionTreeConflict('本树已有待激活或执行中的任务')
                if len(self._turns) >= self.max_turns:
                    raise TurnCapacity('问答暂存容量已满')

            attachments_payload, _ = await self._prepare_and_store_uploads(tree_id, user.userid, uploads)
            agent = await self._runtime.get_agent(tree_id, user=user)
            supplemental = self._build_supplemental(user)
            job = await agent.generate(
                request.question,
                deferred=True,
                parent_id=parent_id,
                advance_head=(request.mode == 'main'),
                attachments=attachments_payload,
                thinking_effort=request.thinking_effort,
                supplemental=supplemental,
                preview=True,
            )

            async with self._condition:
                # 仅置 status='pending_activation' 且 sequence=0，不写伪事件，使激活后 SSE 序号与 agent-core 对齐。
                turn = _Turn(
                    user=user.model_copy(deep=True),
                    tree_id=tree_id,
                    id=job.id,
                    request=request,
                    agent=agent,
                    job=job,
                    expires=utc_now() + timedelta(seconds=self.activation_seconds),
                    deadline=time.monotonic() + self.activation_seconds,
                    parent_id=parent_id,
                    mode=request.mode,
                )
                self._turns[(user.userid, tree_id, turn.id)] = turn
                return TurnCreated(
                    tree_id=tree_id,
                    turn_id=turn.id,
                    activation_expires_at=turn.expires,
                )

    async def _register_redirect(
        self,
        tree_id: str,
        user: AuthenticatedUser,
        request: TurnInput,
        uploads: Sequence[SourceUpload | UploadedFile],
    ) -> TurnCreated:
        async with self._condition:
            old = self._get_locked(tree_id, request.supersedes_turn_id, user.userid)
            if old.status in TERMINAL:
                raise QuestionTreeConflict('已结束任务不能改变方向')
            if old.request is not None and request.mode != old.request.mode:
                raise QuestionTreeConflict('改变方向继承旧任务的推进或探索模式')
            if len(self._turns) >= self.max_turns:
                raise TurnCapacity('问答暂存容量已满')
            old_was_pending = old.status == 'pending_activation'
            old_parent_id = old.parent_id
            old_worker = old.worker

        attachments_payload, saved_refs = await self._prepare_and_store_uploads(tree_id, user.userid, uploads)
        agent = await self._runtime.get_agent(tree_id, user=user)
        supplemental = self._build_supplemental(user)

        if old_was_pending:
            # 尚未激活的旧任务直接取消排队，新任务继承其父节点与主干推进策略。
            try:
                await old.agent.stop(old.id)
            except Exception:
                pass
            new_job = await agent.generate(
                request.question,
                deferred=True,
                parent_id=old_parent_id,
                advance_head=(request.mode == 'main'),
                attachments=attachments_payload,
                thinking_effort=request.thinking_effort,
                supplemental=supplemental,
                preview=True,
            )
            async with self._condition:
                old.superseded_by_turn_id = new_job.id
                if old.status not in TERMINAL:
                    self._set_status_locked(old, 'superseded')
        else:
            # agent-core 原生支持 redirect(..., deferred=True)：立即中断旧任务为 superseded，新任务等待订阅激活。
            try:
                new_job = await agent.redirect(
                    old.id,
                    request.question,
                    deferred=True,
                    attachments=attachments_payload,
                    thinking_effort=request.thinking_effort,
                    supplemental=supplemental,
                    preview=True,
                )
            except StorageConflict as exc:
                for ref in saved_refs:
                    try:
                        await self._files.delete(ref)
                    except Exception:
                        pass
                raise QuestionTreeConflict(str(exc)) from None
            async with self._condition:
                old.superseded_by_turn_id = new_job.id
            # 非取消短等待：避免旧任务正在执行跨平台 A2A 等长耗时工具时卡死改向注册接口及树锁。
            if old_worker is not None and not old_worker.done():
                try:
                    await asyncio.wait_for(asyncio.shield(old_worker), timeout=0.3)
                except (TimeoutError, Exception):
                    pass
            async with self._condition:
                if old.status not in TERMINAL:
                    self._set_status_locked(old, 'superseded')

        async with self._condition:
            replacement = _Turn(
                user=user.model_copy(deep=True),
                tree_id=tree_id,
                id=new_job.id,
                request=request,
                agent=agent,
                job=new_job,
                expires=utc_now() + timedelta(seconds=self.activation_seconds),
                deadline=time.monotonic() + self.activation_seconds,
                supersedes_turn_id=old.id,
                parent_id=old_parent_id if old_was_pending else 'latest',
                mode=request.mode,
            )
            self._turns[(user.userid, tree_id, replacement.id)] = replacement

        return TurnCreated(
            tree_id=tree_id,
            turn_id=replacement.id,
            activation_expires_at=replacement.expires,
            supersedes_turn_id=old.id,
        )

    def _snapshot_locked(self, turn: _Turn) -> TurnSnapshot:
        return TurnSnapshot(
            tree_id=turn.tree_id,
            turn_id=turn.id,
            node_id=turn.node_id,
            summary_id=turn.summary_id,
            status=turn.status,
            can_interrupt=turn.status not in TERMINAL,
            context_status=context_status(turn.status),
            supersedes_turn_id=turn.supersedes_turn_id,
            superseded_by_turn_id=turn.superseded_by_turn_id,
            activation_expires_at=turn.expires,
            activated_at=turn.activated,
            finished_at=turn.finished,
            processing_duration_ms=turn.duration,
            last_sequence=turn.sequence,
            earliest_sequence=turn.events[0]['sequence'] if turn.events else turn.sequence + 1,
            messages=tuple(turn.messages),
            progress=turn.progress,
            error=turn.error,
        )

    async def running_tree_ids(self, owner: str) -> set[str]:
        async with self._condition:
            return {
                turn.tree_id
                for turn in self._turns.values()
                if turn.user.userid == owner
                and turn.status == 'processing'
                and turn.worker is not None
                and not turn.worker.done()
            }

    async def snapshot(self, tree_id: str, turn_id: str, owner: str) -> TurnSnapshot:
        async with self._condition:
            expired = self._cleanup_locked()
            turn = self._get_locked(tree_id, turn_id, owner)
            snap = self._snapshot_locked(turn)
        if expired:
            await self._stop_expired(expired)
        return snap

    @staticmethod
    def _check_cursor(turn: _Turn, cursor: int) -> None:
        if cursor > turn.sequence or (turn.events and cursor < turn.events[0]['sequence'] - 1):
            raise ReplayUnavailable('事件游标不可回放，请先获取任务快照')

    async def subscribe(
        self,
        tree_id: str,
        turn_id: str,
        user: AuthenticatedUser,
        after_sequence: int = 0,
    ):
        async with self.tree_service.operation(tree_id, user.userid):
            async with self._condition:
                expired = self._cleanup_locked()
                turn = self._get_locked(tree_id, turn_id, user.userid)
                if turn.status == 'expired':
                    raise TurnExpired('任务已超过首次激活期限，请重新创建')
                self._check_cursor(turn, after_sequence)
                should_activate = turn.status == 'pending_activation'
                if should_activate:
                    turn.user = user.model_copy(deep=True)
                    turn.node_id = turn.id
                    turn.activated = utc_now()
                    turn.started = time.monotonic()
                    turn.status = 'processing'
            if expired:
                await self._stop_expired(expired)
            if should_activate:
                await turn.job.activate()
                async with self._condition:
                    if turn.worker is None:
                        turn.worker = asyncio.create_task(self._consume(turn))

        async def iterate():
            cursor = after_sequence
            deadline = time.monotonic() + self.heartbeat_seconds
            while True:
                async with self._condition:
                    current = self._get_locked(tree_id, turn_id, user.userid)
                    if current is not turn:
                        raise QuestionTreeNotFound('任务事件源已失效')
                    self._check_cursor(turn, cursor)
                    batch = tuple(e for e in turn.events if e['sequence'] > cursor)
                    terminal = turn.status in TERMINAL
                    if not batch and not terminal and time.monotonic() < deadline:
                        try:
                            await asyncio.wait_for(self._condition.wait(), max(0.01, deadline - time.monotonic()))
                        except TimeoutError:
                            pass
                        continue
                for event in batch:
                    cursor = event['sequence']
                    deadline = time.monotonic() + self.heartbeat_seconds
                    yield event
                if terminal:
                    return
                if not batch:
                    deadline = time.monotonic() + self.heartbeat_seconds
                    yield None

        return iterate()

    async def _read_authoritative_timing(
        self, tree_id: str, node_id: str
    ) -> tuple[datetime | None, datetime | None, int | None]:
        """从 agent-core 持久化 TreeNode 读取权威起止时间与耗时，使终态 SSE 与 POST /open 完全一致。"""
        try:
            view = await self._runtime.try_tree_view(tree_id, include_display=False)
            if view is None:
                return None, None, None
            node = next((n for n in view.nodes if n.id == node_id), None)
            if node is None:
                return None, None, None
            return node.started_at, node.finished_at, node.duration_ms
        except Exception:
            return None, None, None

    async def _consume(self, turn: _Turn) -> None:
        """后台消费 agent-core SessionTask.events() 并按原生序号投影为前端 SSE 白名单事件。"""
        try:
            async for event in turn.job.events():
                etype = event.type
                data = dict(event.data or {})
                ev_seq = getattr(event, 'sequence', None)
                if etype in ('task.completed', 'task.cancelled', 'task.superseded', 'task.failed'):
                    term = etype.removeprefix('task.')
                    started_at, finished_at, duration_ms = await self._read_authoritative_timing(
                        turn.tree_id, turn.id
                    )
                    async with self._condition:
                        if turn.status in TERMINAL:
                            break
                        turn.node_id = turn.id
                        if term == 'failed':
                            turn.error = {
                                'code': 'generation_failed',
                                'message': '问答未完成，请检查附件或稍后重试',
                                'retryable': True,
                            }
                            self._emit_locked(turn, 'error', turn.error)
                        self._set_status_locked(
                            turn,
                            term,
                            sequence=ev_seq,
                            activated=started_at,
                            finished=finished_at,
                            duration=duration_ms,
                        )
                    continue

                async with self._condition:
                    if turn.status in TERMINAL:
                        break
                    if etype == 'task.started':
                        turn.node_id = turn.id
                        self._set_status_locked(turn, 'processing', sequence=ev_seq)
                    elif etype == 'progress':
                        self._emit_locked(
                            turn,
                            'task.progress',
                            normalize_progress_payload(data),
                            sequence=ev_seq,
                        )
                    elif etype == 'message.delta':
                        msg_id = data.get('message_id')
                        if not isinstance(msg_id, str) or not _TREE_ID_RE.match(msg_id):
                            msg_id = new_tree_id()
                        self._emit_locked(
                            turn,
                            'message.delta',
                            {
                                'message_id': msg_id,
                                'message_kind': data.get('message_kind', 'final'),
                                'delta': str(data.get('delta', '')),
                            },
                            sequence=ev_seq,
                        )
                    elif etype == 'message.completed':
                        msg_kind = data.get('message_kind', 'final')
                        text = str(data.get('text', ''))
                        status = data.get('status', 'completed')
                        raw_id = data.get('message_id')
                        if raw_id is None and any(
                            m.message_kind == msg_kind and m.text == text and m.status == 'completed'
                            for m in turn.messages
                        ):
                            continue
                        msg_id = raw_id if isinstance(raw_id, str) and _TREE_ID_RE.match(raw_id) else new_tree_id()
                        self._emit_locked(
                            turn,
                            'message.completed',
                            {
                                'message_id': msg_id,
                                'message_kind': msg_kind,
                                'text': text,
                                'status': status,
                            },
                            sequence=ev_seq,
                        )
            # 确保流结束时若尚未进入终态，按 job.status 收束。
            async with self._condition:
                needs_fallback = turn.status not in TERMINAL
            if needs_fallback:
                started_at, finished_at, duration_ms = await self._read_authoritative_timing(
                    turn.tree_id, turn.id
                )
                async with self._condition:
                    if turn.status not in TERMINAL:
                        mapped = {
                            'completed': 'completed',
                            'cancelled': 'cancelled',
                            'superseded': 'superseded',
                        }.get(turn.job.status, 'failed')
                        if mapped == 'failed' and turn.error is None:
                            turn.error = {
                                'code': 'generation_failed',
                                'message': '问答未完成，请检查附件或稍后重试',
                                'retryable': True,
                            }
                            self._emit_locked(turn, 'error', turn.error)
                        self._set_status_locked(
                            turn,
                            mapped,
                            activated=started_at,
                            finished=finished_at,
                            duration=duration_ms,
                        )
        except asyncio.CancelledError:
            async with self._condition:
                if turn.status not in TERMINAL:
                    self._set_status_locked(turn, 'failed' if self._closed else 'cancelled')
            raise
        except Exception as error:
            logger.warning('探索任务事件消费异常：error_type=%s', type(error).__name__)
            async with self._condition:
                if turn.status not in TERMINAL:
                    turn.error = {
                        'code': 'generation_failed',
                        'message': '问答未完成，请检查附件或稍后重试',
                        'retryable': True,
                    }
                    self._emit_locked(turn, 'error', turn.error)
                    self._set_status_locked(turn, 'failed')
        finally:
            try:
                await self._repository.touch_tree(turn.tree_id, turn.user.userid)
            except Exception:
                pass

    async def cancel(self, tree_id: str, turn_id: str, owner: str) -> TurnSnapshot:
        async with self.tree_service.operation(tree_id, owner):
            async with self._condition:
                expired = self._cleanup_locked()
                turn = self._get_locked(tree_id, turn_id, owner)
                if turn.status in TERMINAL:
                    if turn.status != 'cancelled':
                        raise QuestionTreeConflict('任务已结束')
                    snap = self._snapshot_locked(turn)
                    if expired:
                        await self._stop_expired(expired)
                    return snap
                worker = turn.worker
                agent = turn.agent
            if expired:
                await self._stop_expired(expired)
            try:
                await agent.stop(turn.id)
            except Exception:
                pass
            if worker is not None and not worker.done():
                await asyncio.gather(worker, return_exceptions=True)
            async with self._condition:
                if turn.status not in TERMINAL:
                    self._set_status_locked(turn, 'cancelled')
                return self._snapshot_locked(turn)

    async def remove_tree(self, tree_id: str, owner: str) -> None:
        """调用方持树操作锁；先停止并等待运行中任务，再清理内存记录。"""
        async with self._condition:
            turns = [t for (u, tree, _), t in self._turns.items() if (u, tree) == (owner, tree_id)]
        for turn in turns:
            if turn.status not in TERMINAL:
                try:
                    await turn.agent.stop(turn.id)
                except Exception:
                    pass
        workers = [t.worker for t in turns if t.worker is not None and not t.worker.done()]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self._condition:
            for turn in turns:
                self._turns.pop((owner, tree_id, turn.id), None)
            self._condition.notify_all()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            async with self._condition:
                expired = self._cleanup_locked()
            if expired:
                await self._stop_expired(expired)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        workers = [t.worker for t in self._turns.values() if t.worker is not None and not t.worker.done()]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self._condition:
            self._turns.clear()
            self._condition.notify_all()
