"""先注册、首次订阅激活的探索轮次；SSE 队列驻留当前进程，节点展示历史独立落盘。"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time

import pymupdf
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from app.auth.model import AuthenticatedUser
from app.model.question_tree import new_tree_id, utc_now
from app.model.display_history import DisplayEvent, StoredDisplayHistory
from app.model.question_file import UploadedFile
from app.repository.question_tree import QuestionTreeConflict, QuestionTreeNotFound
from app.schema.exploration_turn import TurnCreated, TurnInput, TurnMessage, TurnSnapshot

logger = logging.getLogger(__name__)
TERMINAL = frozenset({'completed', 'cancelled', 'superseded', 'failed', 'expired'})


class TurnExpired(ValueError):
    pass


class ReplayUnavailable(ValueError):
    pass


class InvalidTurnInput(ValueError):
    pass


class TurnCapacity(RuntimeError):
    pass


@dataclass
class _Turn:
    user: AuthenticatedUser
    tree_id: str
    id: str
    request: TurnInput | None
    uploads: tuple[UploadedFile, ...]
    revision: int
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
    messages: list = field(default_factory=list)
    error: dict | None = None
    node_id: str | None = None
    summary_id: str | None = None
    worker: asyncio.Task | None = None
    supersedes_turn_id: str | None = None
    superseded_by_turn_id: str | None = None
    registered: bool = False
    attachments: tuple = ()
    display_events: list = field(default_factory=list)
    history_failed: bool = False
    progress: dict | None = None


def context_status(status):
    return {'cancelled': 'user_manually_stopped', 'superseded': 'user_goal_adjusted'}.get(status)


def validate_pdfs(uploads):
    """激活后验收实际 PDF；文件后缀和浏览器 MIME 不能替代内容校验。"""
    for upload in uploads:
        try:
            with pymupdf.open(stream=upload.content, filetype='pdf') as document:
                if not document.is_pdf or document.needs_pass or document.page_count < 1:
                    raise ValueError('PDF 不可读取')
        except Exception:
            raise ValueError('PDF 损坏、加密或没有页面') from None


class ExplorationTurnService:
    def __init__(self, trees, tree_service, agent_factory, *, activation_seconds=180,
                 retention_seconds=1800, heartbeat_seconds=15, event_limit=512,
                 max_turns=128, max_staged_bytes=256 * 1024 * 1024):
        self.trees, self.tree_service, self.agent_factory = trees, tree_service, agent_factory
        self.activation_seconds, self.retention_seconds = activation_seconds, retention_seconds
        self.heartbeat_seconds, self.event_limit = heartbeat_seconds, event_limit
        self.max_turns, self.max_staged_bytes = max_turns, max_staged_bytes
        self._turns = {}
        self._condition = asyncio.Condition()
        self._agent = None
        self._closed = False
        self._reaper = None
        self._registrations = set()

    async def start(self):
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._cleanup_loop())

    @staticmethod
    def _project_messages(turn, event, data):
        messages = list(turn.messages)
        if event not in ('message.delta', 'message.completed'):
            return messages
        index = next((i for i, m in enumerate(messages) if m.message_id == data['message_id']), None)
        previous = messages[index] if index is not None else None
        if previous is not None and previous.message_kind != data['message_kind']:
            raise ValueError('公开消息类型不能改变')
        if event == 'message.delta':
            if previous is not None and previous.status != 'streaming':
                raise ValueError('已收束消息不能继续追加')
            message = TurnMessage(message_id=data['message_id'], message_kind=data['message_kind'],
                text=(previous.text if previous else '') + data['delta'], status='streaming')
        else:
            message = TurnMessage.model_validate({k: v for k, v in data.items() if k != 'turn_id'})
        if index is None:
            messages.append(message)
        else:
            messages[index] = message
        return messages

    def _append_event(self, turn, item):
        turn.messages = self._project_messages(turn, item['event'], item['data'])
        turn.sequence = item['sequence']
        turn.events.append(item)
        while len(turn.events) > self.event_limit:
            turn.events.popleft()
        if item['event'] != 'message.delta':
            turn.display_events.append(DisplayEvent.model_validate(item))
        if item['event'] == 'task.progress':
            turn.progress = {key: item['data'][key] for key in ('type', 'message')}
        turn.touched = time.monotonic()
        self._condition.notify_all()

    async def _event(self, turn, event, data):
        item = {'sequence': turn.sequence + 1, 'event': event, 'data': {'turn_id': turn.id, **data}}
        history = StoredDisplayHistory(events=tuple(turn.display_events) + (
            (DisplayEvent.model_validate(item),) if event != 'message.delta' else ()),
            streaming_messages=tuple(m.model_dump() for m in self._project_messages(turn,event,data) if m.status == 'streaming'),
            last_sequence=item['sequence'])
        cancelled = False
        if turn.node_id is not None and not turn.history_failed:
            async def save():
                async with asyncio.timeout(5):
                    await self.trees.save_display_history(turn.tree_id, turn.user.userid, turn.node_id, history)
            # 等待提交完成后再响应取消，避免数据库已有事件而内存游标仍停在上一条。
            saving = asyncio.create_task(save())
            try:
                try:
                    await asyncio.shield(saving)
                except asyncio.CancelledError:
                    cancelled = True
                    await saving
            except Exception as error:
                turn.history_failed = True
                logger.warning('探索展示历史保存失败：error_type=%s', type(error).__name__)
                # 依赖故障只能公开未持久化的失败通知，不能继续宣称完整历史已保存。
                turn.status, turn.finished = 'failed', utc_now()
                turn.request, turn.uploads = None, ()
                turn.error = {'code': 'history_unavailable', 'message': '任务展示历史保存失败，请重新打开问答树核对', 'retryable': True}
                # 存储已不可用，至少收束实时界面的已有预览；不声称这些故障事件已落库。
                for message in tuple(turn.messages):
                    if message.status == 'streaming':
                        self._append_event(turn, {'sequence': turn.sequence + 1, 'event': 'message.completed',
                            'data': {'turn_id': turn.id, **message.model_dump(mode='json'), 'status': 'interrupted'}})
                self._append_event(turn, {'sequence': turn.sequence + 1, 'event': 'error',
                    'data': {'turn_id': turn.id, **turn.error}})
                self._append_event(turn, {'sequence': turn.sequence + 1, 'event': 'turn.status',
                    'data': {'turn_id': turn.id, 'status': 'failed', 'can_interrupt': False,
                             'finished_at': turn.finished.isoformat()}})
                raise
        self._append_event(turn, item)
        if cancelled:
            raise asyncio.CancelledError()

    async def _status(self, turn, status):
        if status in TERMINAL:
            # 在同一条件锁内先收束预览，再公布任务终态；迟到分片不能重新打开消息。
            for message in tuple(turn.messages):
                if message.status == 'streaming':
                    await self._event(turn, 'message.completed', {
                        **message.model_dump(mode='json'), 'status': 'interrupted'})
        turn.status = status
        if status in TERMINAL:
            turn.finished = utc_now()
            turn.duration = int((time.monotonic() - turn.started) * 1000) if turn.started else 0
            turn.request, turn.uploads = None, ()
        await self._event(turn, 'turn.status', {
            'status': status, 'can_interrupt': status not in TERMINAL,
            'context_status': context_status(status),
            'supersedes_turn_id': turn.supersedes_turn_id, 'superseded_by_turn_id': turn.superseded_by_turn_id,
            'created_at': utc_now().isoformat(),
            'activated_at': turn.activated.isoformat() if turn.activated else None,
            'finished_at': turn.finished.isoformat() if turn.finished else None,
            'processing_duration_ms': turn.duration, 'node_id': turn.node_id, 'summary_id': turn.summary_id})

    async def _cleanup(self):
        now = time.monotonic()
        for key, turn in list(self._turns.items()):
            if turn.status == 'pending_activation' and not turn.registered and now >= turn.deadline:
                await self._status(turn, 'expired')
            if turn.status in TERMINAL and now - turn.touched >= self.retention_seconds:
                del self._turns[key]

    async def _get(self, tree_id, turn_id, owner):
        await self._cleanup()
        turn = self._turns.get((owner, tree_id, turn_id))
        if turn is None or self._closed:
            raise QuestionTreeNotFound('问答任务不存在')
        return turn

    async def _selection(self, tree_id, owner, request):
        snapshot = await self.trees.load_tree(tree_id, owner)
        if snapshot.tree.kind != 'exploration':
            raise QuestionTreeNotFound('问答树不存在')
        parent = snapshot.nodes_by_id.get(request.selected_node_id)
        if snapshot.nodes:
            if parent is None or parent.kind != 'task':
                raise QuestionTreeConflict('请选择本树任务节点')
            if parent.status == 'generating':
                raise QuestionTreeConflict('所选任务尚未结束')
            if request.mode == 'main' and (parent.path_type != 'main' or parent.id != snapshot.tree.main_leaf_id):
                raise QuestionTreeConflict('推进模式只能延续当前主干末尾；历史主干或分支节点只能使用探索模式')
        elif request.selected_node_id is not None or request.mode != 'main':
            raise QuestionTreeConflict('空树首轮只能使用推进模式，建立首个主干节点')
        if any(node.status == 'generating' for node in snapshot.nodes):
            raise QuestionTreeConflict('本树已有执行中的任务')
        return snapshot.tree.revision

    async def register(self, tree_id, user, request, uploads=()):
        request = TurnInput.model_validate(request.model_dump())
        # 与打开/删除共用树级锁，先验权威归属，再登记内存任务。所有入口锁顺序一致。
        async with self.tree_service.operation(tree_id, user.userid):
            await self._expire_pending(tree_id, user.userid)
            if request.supersedes_turn_id is not None:
                # 一旦进入替代事务，HTTP 取消也须等待数据库和内存投影一起收尾。
                registering = asyncio.create_task(self._replace(tree_id, user, request, uploads))
                self._registrations.add(registering)
                try:
                    try:
                        return await asyncio.shield(registering)
                    except asyncio.CancelledError:
                        await registering
                        raise
                finally:
                    self._registrations.discard(registering)
            revision = await self._selection(tree_id, user.userid, request)
            async with self._condition:
                await self._cleanup()
                if self._closed:
                    raise RuntimeError('问答任务服务已关闭')
                if any(t.tree_id == tree_id and t.user.userid == user.userid and t.status not in TERMINAL
                       for t in self._turns.values()):
                    raise QuestionTreeConflict('本树已有待激活或执行中的任务')
                staged = sum(len(u.content) for t in self._turns.values() for u in t.uploads)
                if len(self._turns) >= self.max_turns or staged + sum(len(u.content) for u in uploads) > self.max_staged_bytes:
                    raise TurnCapacity('问答暂存容量已满')
                turn = _Turn(user.model_copy(deep=True), tree_id, new_tree_id(), request, tuple(uploads), revision,
                    utc_now() + timedelta(seconds=self.activation_seconds), time.monotonic() + self.activation_seconds)
                self._turns[(user.userid, tree_id, turn.id)] = turn
                await self._status(turn, 'pending_activation')
                return TurnCreated(tree_id=tree_id, turn_id=turn.id, activation_expires_at=turn.expires)

    async def _expire_pending(self, tree_id, owner):
        """调用方持树操作锁；先结束 SQL 中占位的接替节点，再公开过期终态。"""
        async with self._condition:
            pending = [t for t in self._turns.values() if t.tree_id == tree_id and t.user.userid == owner
                       and t.registered and t.status == 'pending_activation' and time.monotonic() >= t.deadline]
        for turn in pending:
            await self._agent.nodes.fail_task(tree_id, owner, turn.id)
            async with self._condition:
                await self._status(turn, 'expired')

    async def _replace_confirmed(self, tree_id, owner, old_id, new_id, question, references):
        try:
            return await self._agent.nodes.replace_task(tree_id, owner, old_id,
                replacement_id=new_id, question=question, attachments=references)
        except Exception:
            # 提交回执丢失时只读核对稳定 ID；不自动重放替代，更不把旧任务记为普通取消。
            try:
                old = await self._agent.nodes.load_node(tree_id, owner, old_id)
                new = await self._agent.nodes.load_node(tree_id, owner, new_id)
                if (old.node.status == 'superseded' and old.node.superseded_by_node_id == new_id
                        and new.node.parent_id == old_id and new.node.status == 'generating'
                        and new.content.question == question and new.content.attachments == list(references)):
                    return new
            except Exception:
                pass
            raise

    async def _replace(self, tree_id, user, request, uploads):
        async with self._condition:
            old = await self._get(tree_id, request.supersedes_turn_id, user.userid)
            if old.status in TERMINAL:
                raise QuestionTreeConflict('已结束任务不能改变方向')
            original_request = old.request
            if request.mode != original_request.mode:
                raise QuestionTreeConflict('改变方向继承旧任务的推进或探索模式')
            if old.status == 'processing' and old.node_id is None:
                raise QuestionTreeConflict('任务尚在准备上下文，请待节点就绪后改变方向')
            if len(self._turns) >= self.max_turns:
                raise TurnCapacity('问答暂存容量已满')
            retained = sum(len(u.content) for t in self._turns.values() if t is not old for u in t.uploads)
            if retained + sum(len(u.content) for u in uploads) > self.max_staged_bytes:
                raise TurnCapacity('问答暂存容量已满')
        # 先验新附件；非法新输入、权限错误、容量不足都不终止旧任务。
        try:
            await run_in_threadpool(validate_pdfs, uploads)
        except ValueError:
            raise InvalidTurnInput('替代任务 PDF 不合法') from None
        snapshot = await self.trees.load_tree(tree_id, user.userid)
        if snapshot.tree.kind != 'exploration':
            raise QuestionTreeNotFound('问答树不存在')
        references = ()
        turn_id = new_tree_id()
        registered = old.node_id is not None
        if registered:
            current = snapshot.nodes_by_id.get(old.node_id)
            if current is None or current.status != 'generating' or current.path_type != request.mode:
                raise QuestionTreeConflict('旧任务已经结束或路径发生变化')
            if any(n.status == 'generating' and n.id != old.node_id for n in snapshot.nodes):
                raise QuestionTreeConflict('本树已有其他执行任务')
            if uploads:
                references = tuple(await self._agent.file_service.prepare(tree_id, user.userid, uploads=uploads))
        else:
            # 尚未激活的任务没有 SQL 节点；接替它仍从原选择起点继续。
            await self._selection(tree_id, user.userid, original_request)
        replacement_committed = False
        try:
            async with self._condition:
                # 保存附件期间旧任务可能刚好完成；重新验收，再以 SQL 树锁裁决提交竞态。
                if self._closed:
                    raise RuntimeError('问答任务服务已关闭')
                if old.status in TERMINAL:
                    raise QuestionTreeConflict('旧任务已经结束')
                retained = sum(len(u.content) for t in self._turns.values() if t is not old for u in t.uploads)
                if len(self._turns) >= self.max_turns or retained + sum(len(u.content) for u in uploads) > self.max_staged_bytes:
                    raise TurnCapacity('问答暂存容量已满')
                effective = request.model_copy(update={'selected_node_id': old.id if registered else original_request.selected_node_id})
                replacement = _Turn(user.model_copy(deep=True), tree_id, turn_id, effective,
                    () if registered else tuple(uploads), snapshot.tree.revision,
                    utc_now() + timedelta(seconds=self.activation_seconds), time.monotonic() + self.activation_seconds,
                    supersedes_turn_id=old.id, registered=registered, attachments=references)
                if registered:
                    # 持短时事件锁跨原子替代，旧生产者不会在 SQL 已替代后先发布 failed/cancelled。
                    restored = await self._replace_confirmed(tree_id, user.userid, old.id,
                        turn_id, request.question, references)
                    replacement.revision, replacement.node_id = restored.tree.revision, restored.node.id
                    replacement_committed = True
                # SQL 已确认后先纳入生命周期管理，展示保存失败也不能遗留无人管理的占位节点。
                self._turns[(user.userid, tree_id, turn_id)] = replacement
                old.superseded_by_turn_id = turn_id
                await self._status(old, 'superseded')
                await self._status(replacement, 'pending_activation')
        except BaseException:
            if replacement_committed:
                if old.worker is not None and not old.worker.done():
                    old.worker.cancel()
                    await self._wait_workers([old.worker])
                try:
                    await self._agent.nodes.fail_task(tree_id, user.userid, turn_id)
                    async with self._condition:
                        await self._status(replacement, 'failed')
                except Exception as error:
                    logger.warning('替代任务展示失败后清理未确认：error_type=%s', type(error).__name__)
            if references:
                await self._agent.file_service.cleanup_unregistered(tree_id, user.userid, references)
            raise
        if old.worker is not None and not old.worker.done():
            old.worker.cancel()
            await self._wait_workers([old.worker])
        return TurnCreated(tree_id=tree_id, turn_id=turn_id, activation_expires_at=replacement.expires,
                           supersedes_turn_id=old.id)

    def _snapshot(self, turn):
        return TurnSnapshot(tree_id=turn.tree_id, turn_id=turn.id, node_id=turn.node_id, summary_id=turn.summary_id,
            status=turn.status, can_interrupt=turn.status not in TERMINAL,
            context_status=context_status(turn.status),
            supersedes_turn_id=turn.supersedes_turn_id, superseded_by_turn_id=turn.superseded_by_turn_id,
            activation_expires_at=turn.expires, activated_at=turn.activated, finished_at=turn.finished,
            processing_duration_ms=turn.duration, last_sequence=turn.sequence,
            earliest_sequence=turn.events[0]['sequence'] if turn.events else turn.sequence + 1,
            messages=tuple(turn.messages), progress=turn.progress, error=turn.error)

    async def running_tree_ids(self, owner: str) -> set[str]:
        """一次读取本人正在执行的树；不激活任务，也不加载树节点。"""
        async with self._condition:
            # SQL generating 也可能是待激活接替节点或崩溃遗留，不能据此断言仍在执行。
            return {turn.tree_id for turn in self._turns.values()
                    if turn.user.userid == owner and turn.status == 'processing'
                    and turn.worker is not None and not turn.worker.done()}

    async def snapshot(self, tree_id, turn_id, owner):
        async with self._condition:
            turn = await self._get(tree_id, turn_id, owner)
            if not (turn.registered and turn.status == 'pending_activation' and time.monotonic() >= turn.deadline):
                return self._snapshot(turn)
        async with self.tree_service.operation(tree_id, owner):
            await self._expire_pending(tree_id, owner)
            async with self._condition:
                return self._snapshot(await self._get(tree_id, turn_id, owner))

    def _cursor(self, turn, cursor):
        if cursor > turn.sequence or (turn.events and cursor < turn.events[0]['sequence'] - 1):
            raise ReplayUnavailable('事件游标不可回放，请先获取任务快照')

    async def subscribe(self, tree_id, turn_id, user, after_sequence=0, *, access_token: SecretStr | None = None):
        if access_token is not None and not isinstance(access_token,SecretStr):
            raise TypeError('激活凭据必须使用SecretStr')
        async with self.tree_service.operation(tree_id, user.userid):
            await self._expire_pending(tree_id, user.userid)
            async with self._condition:
                turn = await self._get(tree_id, turn_id, user.userid)
                if turn.status == 'expired':
                    raise TurnExpired('任务已超过首次激活期限，请重新创建')
                self._cursor(turn, after_sequence)
                pending = turn.status == 'pending_activation'
                request = turn.request
            if pending:
                # 数据库读取不持有全局事件条件锁，其他树的输出、心跳和取消继续运行。
                if turn.registered:
                    restored = await self._agent.nodes.load_node(tree_id, user.userid, turn.id)
                    if restored.node.status != 'generating':
                        raise QuestionTreeConflict('接替节点已经结束')
                    revision = restored.tree.revision
                else:
                    revision = await self._selection(tree_id, user.userid, request)
                await self._expire_pending(tree_id, user.userid)
                async with self._condition:
                    turn = await self._get(tree_id, turn_id, user.userid)
                    if turn.status == 'expired':
                        raise TurnExpired('任务已超过首次激活期限，请重新创建')
                    self._cursor(turn, after_sequence)
                    if revision != turn.revision:
                        raise QuestionTreeConflict('树已变化，请取消待激活任务并重新创建')
                    # 首次订阅重新取得中央身份；Bearer只交给本轮生产者，不进入Turn、事件或数据库。
                    turn.user = user.model_copy(deep=True)
                    turn.activated, turn.started = utc_now(), time.monotonic()
                    await self._status(turn, 'processing')
                    turn.worker = asyncio.create_task(self._run(turn, access_token=access_token))

        async def iterate():
            cursor = after_sequence
            deadline = time.monotonic() + self.heartbeat_seconds
            while True:
                async with self._condition:
                    current = await self._get(tree_id, turn_id, user.userid)
                    if current is not turn:
                        raise QuestionTreeNotFound('任务事件源已失效')
                    self._cursor(turn, cursor)
                    batch = tuple(e for e in turn.events if e['sequence'] > cursor)
                    terminal = turn.status in TERMINAL
                    if not batch and not terminal and time.monotonic() < deadline:
                        try:
                            await asyncio.wait_for(self._condition.wait(), deadline - time.monotonic())
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

    async def _settle(self, turn, fallback):
        """取消与最终提交交错时，以 SQL 终态为准，不能把已完成任务改成取消。"""
        restored = None
        status = fallback
        if turn.node_id is not None and self._agent is not None:
            try:
                async with asyncio.timeout(5):
                    restored = await self._agent.nodes.load_node(turn.tree_id, turn.user.userid, turn.node_id)
                state = restored.node.status
                status = {'complete': 'completed', 'cancelled': 'cancelled', 'failed': 'failed', 'superseded': 'superseded'}.get(state, 'failed')
            except Exception as error:
                status = 'completed' if fallback == 'completed' else 'failed'
                logger.warning('探索任务终态核对未确认：error_type=%s', type(error).__name__)
        async with self._condition:
            if turn.status != 'processing':
                return
            if restored is not None:
                seen = {message.message_id for message in turn.messages if message.status == 'completed'}
                for message in restored.messages:
                    if message.kind in ('intermediate', 'final') and message.status == 'completed' and message.id not in seen:
                        public = TurnMessage(message_id=message.id, message_kind=message.kind, text=message.text)
                        await self._event(turn, 'message.completed', public.model_dump(mode='json'))
            if status == 'superseded' and restored is not None:
                turn.superseded_by_turn_id = restored.node.superseded_by_node_id
            await self._status(turn, status)

    async def _run(self, turn, *, access_token: SecretStr | None = None):
        try:
            await run_in_threadpool(validate_pdfs, turn.uploads)
            if self._agent is None:
                self._agent = self.agent_factory()
            agent = self._agent
            request = turn.request
            async def publish(kind, text):
                # 只发布已提交的公开输出；消息 ID 与打开树时的 SQL 历史保持一致。
                restored = await agent.nodes.load_node(turn.tree_id, turn.user.userid, turn.id)
                message = next(m for m in reversed(restored.messages) if m.kind == kind and m.text == text)
                async with self._condition:
                    if turn.status != 'processing':
                        return
                    turn.node_id = turn.id
                    public = TurnMessage(message_id=message.id, message_kind=kind, text=text)
                    # 不带流式回调的内部宿主仍可一次交付已提交正文。
                    await self._event(turn, 'message.delta', {'message_id': message.id, 'message_kind': kind, 'delta': text})
                    await self._event(turn, 'message.completed', public.model_dump(mode='json'))
            async def publish_stream(event, data):
                async with self._condition:
                    if turn.status != 'processing':
                        return
                    await self._event(turn, event, data)
            async def progress(value):
                async with self._condition:
                    if turn.status == 'processing':
                        await self._event(turn, 'task.progress', {'type': value.type, 'message': value.message})
            async def registered(restored, summary_id):
                async with self._condition:
                    turn.node_id, turn.summary_id = restored.node.id, summary_id
                    await self._status(turn, 'processing')
            result = await agent.generate(user=turn.user, tree_id=turn.tree_id, node_id=turn.id,
                selected_node_id=request.selected_node_id, question=request.question,
                advance_main=request.mode == 'main', expected_revision=turn.revision,
                thinking_effort=request.thinking_effort, uploads=turn.uploads, attachments=turn.attachments,
                publish=publish, publish_stream=publish_stream, publish_progress=progress, on_registered=registered, resume_registered=turn.registered,
                **({'access_token':access_token} if access_token is not None else {}))
            async with self._condition:
                turn.node_id, turn.summary_id = result.node_id, result.summary_id
            if getattr(getattr(result, 'result', None), 'delivery_error_type', None):
                await self._settle(turn, 'completed')
            else:
                async with self._condition:
                    if turn.status == 'processing':
                        await self._status(turn, 'completed')
        except asyncio.CancelledError:
            await self._settle(turn, 'failed' if self._closed else 'cancelled')
            raise
        except Exception as error:
            logger.warning('探索问答执行失败：error_type=%s', type(error).__name__)
            async with self._condition:
                if turn.status == 'processing':
                    turn.error = {'code': 'generation_failed', 'message': '问答未完成，请检查附件或稍后重试', 'retryable': True}
                    await self._event(turn, 'error', turn.error)
            # 提交回执或完成交付失败时回查 SQL；已保存答案不能被当成中断预览。
            await self._settle(turn, 'failed')

        finally:
            # 重连只订阅，不替换正在执行任务的凭据；终态立即释放生产者对token的引用。
            access_token=None

    async def _wait_workers(self, workers):
        waiting = asyncio.gather(*workers, return_exceptions=True)
        try:
            await asyncio.shield(waiting)
        except asyncio.CancelledError:
            # HTTP 取消不能再次打断生产者的 SQL/文件补偿；完成等待后才释放树级锁。
            await waiting
            raise

    async def cancel(self, tree_id, turn_id, owner):
        async with self.tree_service.operation(tree_id, owner):
            await self._expire_pending(tree_id, owner)
            async with self._condition:
                turn = await self._get(tree_id, turn_id, owner)
                if turn.status in TERMINAL:
                    if turn.status != 'cancelled':
                        raise QuestionTreeConflict('任务已结束')
                    return self._snapshot(turn)
                worker = turn.worker
                if worker is None:
                    if turn.registered:
                        await self._agent.nodes.cancel_task(tree_id, owner, turn.id)
                    await self._status(turn, 'cancelled')
            if worker is not None:
                worker.cancel()
                await self._wait_workers([worker])
            async with self._condition:
                # create_task 尚未获得运行时间就被取消时，协程内部的 finally 不会执行。
                if turn.status == 'processing':
                    await self._status(turn, 'cancelled')
                return self._snapshot(turn)

    async def remove_tree(self, tree_id, owner):
        """调用方持树操作锁；先停止并等待生产者，再允许 SQL/附件删除。"""
        async with self._condition:
            turns = [t for (u, tree, _), t in self._turns.items() if (u, tree) == (owner, tree_id)]
            workers = [t.worker for t in turns if t.worker and not t.worker.done()]
            for worker in workers:
                worker.cancel()
        await self._wait_workers(workers)
        if self._agent is not None:
            self._agent.release_tree(tree_id, owner)
        async with self._condition:
            for t in turns:
                self._turns.pop((owner, tree_id, t.id), None)
            self._condition.notify_all()

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(1)
            async with self._condition:
                await self._cleanup()
                keys = {(t.tree_id, t.user.userid) for t in self._turns.values()
                        if t.registered and t.status == 'pending_activation' and time.monotonic() >= t.deadline}
            for tree_id, owner in keys:
                try:
                    async with self.tree_service.operation(tree_id, owner):
                        await self._expire_pending(tree_id, owner)
                except Exception as error:
                    logger.warning('待激活接替节点过期清理未确认：error_type=%s', type(error).__name__)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reaper:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        # 替代注册已进入 SQL 时先等其投影完成，再清理新产生的待激活节点。
        if self._registrations:
            await asyncio.gather(*tuple(self._registrations), return_exceptions=True)
        if self._agent is not None:
            for turn in tuple(self._turns.values()):
                if turn.registered and turn.status == 'pending_activation':
                    try:
                        await self._agent.nodes.fail_task(turn.tree_id, turn.user.userid, turn.id)
                        async with self._condition:
                            await self._status(turn, 'failed')
                    except Exception as error:
                        logger.warning('待激活接替节点停机清理未确认：error_type=%s', type(error).__name__)
            await self._agent.close()
        workers = [t.worker for t in self._turns.values() if t.worker and not t.worker.done()]
        for worker in workers:
            worker.cancel()
        await self._wait_workers(workers)
        async with self._condition:
            self._turns.clear()
            self._condition.notify_all()
