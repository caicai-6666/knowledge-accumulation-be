"""基于 agent-core 的探索树业务服务：建树、列表、改名、打开全量视图与删树。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import re
from typing import Any
from uuid import NAMESPACE_OID, uuid5

from agent_core import TreeNode, TreeView

from app.infrastructure.upload_file import UploadFileStore
from app.model.display_history import DisplayEvent, StreamingMessage, TaskDisplayHistory
from app.model.question_file import AttachmentReference
from app.model.question_tree import QuestionTreeListItem
from app.repository.exploration_tree import ExplorationTreeRecord, ExplorationTreeRepository
from app.schema.exploration import (
    ExplorationNodeResponse,
    ExplorationTreeListItem,
    ExplorationTreeResponse,
    MessageDisplay,
    TaskDisplayContent,
)

_TREE_ID_RE = re.compile(r'^[a-f0-9]{32}$')
_VALID_PROGRESS_TYPES = frozenset({'thinking', 'online-search', 'external-expert', 'local-search', 'a2a', 'boardcast'})

_STATUS_MAP = {
    'running': 'generating',
    'generating': 'generating',
    'pending_activation': 'generating',
    'queued': 'generating',
    'complete': 'complete',
    'completed': 'complete',
    'cancelled': 'cancelled',
    'superseded': 'superseded',
    'failed': 'failed',
}


def _stable_tree_id(raw: str | None, fallback_seed: str) -> str:
    if isinstance(raw, str) and _TREE_ID_RE.match(raw):
        return raw
    return uuid5(NAMESPACE_OID, fallback_seed).hex


def _normalize_status(status: str) -> str:
    return _STATUS_MAP.get(status, 'failed')


def _context_status(status: str) -> str | None:
    if status == 'cancelled':
        return 'user_manually_stopped'
    if status == 'superseded':
        return 'user_goal_adjusted'
    return None


def normalize_progress_payload(data: dict[str, Any]) -> dict[str, str]:
    """将 agent-core 进度事件统一规范为前端白名单字段（type + 以「正在」开头的 message）。"""
    raw_type = data.get('type')
    progress_type = raw_type if raw_type in _VALID_PROGRESS_TYPES else 'thinking'
    raw_message = str(data.get('message') or '').strip()
    if not raw_message:
        raw_message = '正在处理当前任务'
    elif not raw_message.startswith('正在'):
        raw_message = f'正在{raw_message}'
    if len(raw_message) > 2000:
        raw_message = raw_message[:2000]
    return {'type': progress_type, 'message': raw_message}


def _parse_attachments(raw_items: list[dict[str, Any]]) -> tuple[AttachmentReference, ...]:
    result: list[AttachmentReference] = []
    for item in raw_items or ():
        if not isinstance(item, dict):
            continue
        file_id = item.get('file_id')
        file_type = item.get('type', 'images')
        if isinstance(file_id, str):
            try:
                result.append(AttachmentReference(type=file_type, file_id=file_id))
            except Exception:
                continue
    return tuple(result)


def _project_recorded_display(
    node: TreeNode,
    superseded_by_node_id: str | None,
) -> TaskDisplayHistory | None:
    """将 agent-core 原生 TreeNode.display 映射为前端白名单 TaskDisplayHistory。"""
    raw_display = getattr(node, 'display', None)
    if raw_display is None:
        return None
    if not (raw_display.events or raw_display.streaming_messages or raw_display.last_sequence > 0):
        return None

    activated_iso = (node.started_at or node.created_at).isoformat()
    created_iso = node.created_at.isoformat()
    finished_iso = (node.finished_at or node.created_at).isoformat()

    mapped_events: list[DisplayEvent] = []
    completed_pairs: set[tuple[str, str]] = set()
    completed_ids: set[str] = set()
    for ev in raw_display.events:
        etype = ev.type
        data = dict(ev.data or {})
        if etype == 'task.started':
            mapped_events.append(
                DisplayEvent(
                    sequence=ev.sequence,
                    event='turn.status',
                    data={
                        'turn_id': node.id,
                        'status': 'processing',
                        'can_interrupt': True,
                        'context_status': None,
                        'supersedes_turn_id': None,
                        'superseded_by_turn_id': superseded_by_node_id,
                        'created_at': created_iso,
                        'activated_at': activated_iso,
                        'finished_at': None,
                        'processing_duration_ms': None,
                        'node_id': node.id,
                        'summary_id': None,
                    },
                )
            )
        elif etype == 'progress':
            mapped_events.append(
                DisplayEvent(
                    sequence=ev.sequence,
                    event='task.progress',
                    data={'turn_id': node.id, **normalize_progress_payload(data)},
                )
            )
        elif etype == 'message.completed':
            msg_kind = 'intermediate' if data.get('message_kind') == 'intermediate' else 'final'
            text = str(data.get('text', ''))
            msg_status = 'interrupted' if data.get('status') == 'interrupted' else 'completed'
            raw_id = data.get('message_id')
            # 跳过无 message_id 且内容与已记录完成事件重复的 committed 回执。
            if raw_id is None and msg_status == 'completed' and (msg_kind, text) in completed_pairs:
                continue
            msg_id = _stable_tree_id(raw_id, f'{node.id}:display_msg:{ev.sequence}')
            completed_ids.add(msg_id)
            if msg_status == 'completed':
                completed_pairs.add((msg_kind, text))
            mapped_events.append(
                DisplayEvent(
                    sequence=ev.sequence,
                    event='message.completed',
                    data={
                        'turn_id': node.id,
                        'message_id': msg_id,
                        'message_kind': msg_kind,
                        'text': text,
                        'status': msg_status,
                    },
                )
            )
        elif etype in ('task.completed', 'task.cancelled', 'task.superseded', 'task.failed'):
            term = etype.removeprefix('task.')
            mapped_events.append(
                DisplayEvent(
                    sequence=ev.sequence,
                    event='turn.status',
                    data={
                        'turn_id': node.id,
                        'status': term,
                        'can_interrupt': False,
                        'context_status': _context_status(term),
                        'supersedes_turn_id': None,
                        'superseded_by_turn_id': superseded_by_node_id,
                        'created_at': created_iso,
                        'activated_at': activated_iso,
                        'finished_at': finished_iso,
                        'processing_duration_ms': node.duration_ms if node.duration_ms is not None else 0,
                        'node_id': node.id,
                        'summary_id': None,
                    },
                )
            )
        # 其他内部事件（如 memory.deferred）跳过，不进入前端公开展示历史。

    streaming_messages: list[StreamingMessage] = []
    seen_streaming: set[str] = set()
    for msg in raw_display.streaming_messages:
        if msg.message_id in completed_ids or msg.message_id in seen_streaming:
            continue
        if not _TREE_ID_RE.match(msg.message_id):
            continue
        seen_streaming.add(msg.message_id)
        streaming_messages.append(
            StreamingMessage(
                message_id=msg.message_id,
                message_kind='intermediate' if msg.message_kind == 'intermediate' else 'final',
                text=msg.text,
            )
        )

    last_seq = max(raw_display.last_sequence, mapped_events[-1].sequence if mapped_events else 0)
    return TaskDisplayHistory(
        events=tuple(mapped_events),
        streaming_messages=tuple(streaming_messages),
        last_sequence=last_seq,
        event_source='recorded',
    )


def render_tree_view(record: ExplorationTreeRecord, view: TreeView) -> ExplorationTreeResponse:
    """将 agent-core TreeView（含原生主干 head、耗时与 display）转换为前端期望的 ExplorationTreeResponse。"""
    nodes_by_id: dict[str, TreeNode] = {node.id: node for node in view.nodes}
    children_by_parent: dict[str | None, list[str]] = {}
    for node in view.nodes:
        children_by_parent.setdefault(node.parent_id, []).append(node.id)

    # 首轮或后续主干任务生成中时 TreeView.head 尚未推进，仅依赖 view.head 会把在途主干节点误判为 branch；
    # 因此当存在活跃节点且其 advance_head=True 时，以该活跃节点作为当前有效主干末尾。
    active_node = nodes_by_id.get(view.active) if view.active is not None else None
    effective_main_leaf = (
        active_node.id
        if active_node is not None and active_node.advance_head
        else view.head
    )

    main_ids: set[str] = set()
    cursor = effective_main_leaf
    while cursor is not None and cursor in nodes_by_id and cursor not in main_ids:
        main_ids.add(cursor)
        cursor = nodes_by_id[cursor].parent_id

    # 为 status == 'superseded' 的节点推导接替节点（优先取位于主干上的直接子节点）。
    superseded_by: dict[str, str] = {}
    for node in view.nodes:
        if _normalize_status(node.status) == 'superseded':
            children = children_by_parent.get(node.id, [])
            if children:
                preferred = next((cid for cid in children if cid in main_ids), children[0])
                superseded_by[node.id] = preferred

    rendered_nodes: list[ExplorationNodeResponse] = []
    for seq, node in enumerate(view.nodes, start=1):
        status = _normalize_status(node.status)
        path_type = 'main' if node.id in main_ids else 'branch'
        attachments = _parse_attachments(node.attachments)

        messages: list[MessageDisplay] = [
            MessageDisplay(
                id=_stable_tree_id(None, f'{node.id}:question'),
                kind='question',
                status='completed',
                text=node.question,
                sequence=1,
                created_at=node.created_at,
            )
        ]
        output_ids = list(node.output_ids or ())
        last_final = ''
        for idx, pair in enumerate(node.outputs or ()):
            raw_kind, text = pair
            msg_kind = 'intermediate' if raw_kind == 'intermediate' else 'final'
            if msg_kind == 'final':
                last_final = text
            raw_id = output_ids[idx] if idx < len(output_ids) else None
            msg_id = _stable_tree_id(raw_id, f'{node.id}:output:{idx}')
            messages.append(
                MessageDisplay(
                    id=msg_id,
                    kind=msg_kind,
                    status='completed',
                    text=text,
                    sequence=idx + 2,
                    created_at=node.created_at,
                )
            )

        answer = node.answer if isinstance(node.answer, str) else last_final
        recorded = _project_recorded_display(node, superseded_by.get(node.id))
        if recorded is not None:
            display = recorded
        else:
            display = TaskDisplayHistory(
                event_source='legacy',
                events=tuple(
                    DisplayEvent(
                        event='message.completed',
                        data={
                            'turn_id': node.id,
                            'message_id': msg.id,
                            'message_kind': msg.kind,
                            'text': msg.text,
                            'status': msg.status,
                        },
                    )
                    for msg in messages
                    if msg.kind != 'question'
                ),
            )

        created_at = node.started_at or node.created_at
        finished_at = None if status == 'generating' else (node.finished_at or node.created_at)
        processing_duration_ms = None if status == 'generating' else node.duration_ms

        rendered_nodes.append(
            ExplorationNodeResponse(
                id=node.id,
                parent_id=node.parent_id,
                children_ids=tuple(children_by_parent.get(node.id, ())),
                kind='task',
                path_type=path_type,
                status=status,
                sequence=seq,
                created_at=created_at,
                finished_at=finished_at,
                superseded_by_node_id=superseded_by.get(node.id),
                context_status=_context_status(status),
                can_interrupt=(status == 'generating'),
                processing_duration_ms=processing_duration_ms,
                content=TaskDisplayContent(
                    question=node.question,
                    attachments=attachments,
                    answer=answer,
                ),
                messages=tuple(messages),
                display=display,
            )
        )

    roots = children_by_parent.get(None, ())
    return ExplorationTreeResponse(
        id=record.tree_id,
        name=record.name,
        kind='exploration',
        root_id=roots[0] if roots else None,
        main_leaf_id=effective_main_leaf,
        revision=len(view.nodes),
        created_at=record.created_at,
        updated_at=record.updated_at,
        nodes=tuple(rendered_nodes),
    )


class ExplorationTreeService:
    """探索树全生命周期协调服务。"""

    def __init__(
        self,
        repository: ExplorationTreeRepository,
        runtime: Any,
        *,
        upload_files: UploadFileStore | None = None,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._files = upload_files if upload_files is not None else UploadFileStore()
        self._closed = False
        self._operations: dict[tuple[str, str], list[Any]] = {}
        self.turns: Any = None

    @asynccontextmanager
    async def _operation(self, key: tuple[str, str]):
        state = self._operations.setdefault(key, [asyncio.Lock(), 0])
        state[1] += 1
        try:
            async with state[0]:
                if self._closed:
                    raise RuntimeError('探索树服务已关闭')
                yield
        finally:
            state[1] -= 1
            if not state[1]:
                self._operations.pop(key, None)

    def operation(self, tree_id: str, owner_userid: str):
        """同树打开、任务注册/激活/取消及删除共用协调锁；不同树互不等待。"""
        return self._operation((owner_userid, tree_id))

    async def create_tree(self, owner_userid: str, name: str) -> QuestionTreeListItem:
        record = await self._repository.create_tree(owner_userid, name)
        return QuestionTreeListItem(id=record.tree_id, name=record.name)

    async def rename_tree(self, tree_id: str, owner_userid: str, name: str) -> QuestionTreeListItem:
        record = await self._repository.rename_tree(tree_id, owner_userid, name)
        return QuestionTreeListItem(id=record.tree_id, name=record.name)

    async def list_trees(self, owner_userid: str) -> list[ExplorationTreeListItem]:
        records = await self._repository.list_trees(owner_userid)
        running: set[str] = set()
        if self.turns is not None:
            running = await self.turns.running_tree_ids(owner_userid)
        items: list[ExplorationTreeListItem] = []
        for record in records:
            has_running = record.tree_id in running
            if not has_running and self._runtime is not None:
                view = await self._runtime.try_tree_view(record.tree_id, include_display=False)
                if view is not None and view.active is not None:
                    has_running = True
            items.append(
                ExplorationTreeListItem(
                    id=record.tree_id,
                    name=record.name,
                    updated_at=record.updated_at,
                    has_running_task=has_running,
                )
            )
        return items

    async def open_tree(self, tree_id: str, owner_userid: str) -> ExplorationTreeResponse:
        async with self._operation((owner_userid, tree_id)):
            record = await self._repository.get_tree(tree_id, owner_userid)
            view = await self._runtime.try_tree_view(tree_id, include_display=True)
            if view is None:
                view = TreeView(
                    tree_id=tree_id,
                    head=None,
                    active=None,
                    workspace_retention='per_node',
                    nodes=[],
                )
            return render_tree_view(record, view)

    async def delete_tree(self, tree_id: str, owner_userid: str) -> None:
        async with self._operation((owner_userid, tree_id)):
            await self._repository.get_tree(tree_id, owner_userid)
            if self.turns is not None:
                await self.turns.remove_tree(tree_id, owner_userid)
            if self._runtime is not None:
                await self._runtime.close_tree(tree_id)
            file_ids = await self._repository.delete_tree_data(tree_id, owner_userid)
            for file_id in file_ids:
                try:
                    await self._files.delete(AttachmentReference(type='images', file_id=file_id))
                except Exception:
                    # 单个磁盘文件异常不阻断其余文件清理，但保留后续排障可能。
                    pass

    async def close(self) -> None:
        self._closed = True
