"""问答树异步仓库；所有读取按用户隔离，连接按需建立，不自动建表。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timezone
import json

import asyncmy
from asyncmy.cursors import DictCursor
from pydantic import TypeAdapter

from app.config.database import MySQLSettings, load_mysql_settings
from app.model.question_memory import MemoryEmbeddingJob, QuestionNodeMemory, SearchableMemory, decode_embedding, encode_embedding, normalize_embedding
from app.model.node_persistence import NodeMessage
from app.model.display_history import StoredDisplayHistory
from app.model.question_tree import (
    NodeContent, NodeWrite, QuestionNode, QuestionTree, QuestionTreeListItem, QuestionTreeListEntry, SummaryContent,
    TaskContent, TreeID, TreeKind, WorkspaceVersion, utc_now,
)
from app.service.question_tree import ContextPath, LoadedQuestionTree, QuestionTreeSnapshot
from app.service.question_tree_cache import QuestionTreeCache
from app.service.assistant_memory import AssistantMemoryCache

_CONTENT = TypeAdapter(NodeContent)

# 只处理终态任务的完整公开消息；取消节点排除输出，私有生成与摘要从不进入扫描。
_EMBEDDING_SOURCE = (
    'SELECT m.*, n.tree_id, t.owner_userid, s.text AS source_text '
    'FROM question_node_memories AS m '
    'JOIN question_nodes AS n ON n.id=m.node_id '
    'JOIN question_trees AS t ON t.id=n.tree_id '
    'JOIN question_node_messages AS s ON s.id=m.source_id AND s.node_id=m.node_id AND s.kind=m.kind '
    "WHERE n.kind='task' AND n.status IN ('complete','cancelled','superseded','failed') "
    "AND s.status='completed' AND CHAR_LENGTH(TRIM(s.text))>0 "
    "AND m.embedding_status IN ('pending','failed') "
    "AND (n.status<>'cancelled' OR m.kind='question')"
)


class QuestionTreeNotFound(LookupError):
    """树不存在或不属于当前用户，统一处理以避免泄漏归属。"""


class QuestionTreeConflict(ValueError):
    """调用方持有旧版本，需重新读取后决定是否重试。"""


def _decode(value):
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _row(model, row):
    # MySQL DATETIME 不携带时区；本仓库统一按 UTC 写入和恢复。
    values = dict(row)
    for key in ('created_at', 'updated_at', 'embedded_at', 'finished_at'):
        if key in values and values[key] is not None:
            values[key] = values[key].replace(tzinfo=timezone.utc)
    return model.model_validate(values)


def _date(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class MySQLQuestionTreeRepository:
    def __init__(self, settings: MySQLSettings | None = None):
        self._settings = settings
        self._pool = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.structure_cache = QuestionTreeCache()
        self._snapshot_transactions = {}
        self.assistant_memory_cache = AssistantMemoryCache()
        self._assistant_transactions = {}

    async def _initialize(self):
        async with self._lock:
            if self._closed:
                raise RuntimeError('问答树仓库已关闭')
            if self._pool is None:
                settings = self._settings or load_mysql_settings()
                self._settings = settings
                async with asyncio.timeout(settings.operation_timeout_seconds):
                    self._pool = await asyncmy.create_pool(
                        host=settings.host, port=settings.port, user=settings.user,
                        password=settings.password.get_secret_value(), db=settings.database,
                        charset='utf8mb4', autocommit=False, minsize=1, maxsize=5,
                        connect_timeout=settings.connect_timeout_seconds, pool_recycle=300,
                        init_command='SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')

    @asynccontextmanager
    async def _transaction(self):
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection:
                await connection.begin()
                pending = {}
                assistant_pending = {}
                assistant_epoch = self.assistant_memory_cache.epoch
                epoch = self.structure_cache.epoch
                cursor_id = None
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        cursor_id = id(cursor)
                        self._snapshot_transactions[cursor_id] = pending
                        self._assistant_transactions[cursor_id] = assistant_pending
                        yield cursor
                    await connection.commit()
                    # SQL 提交成功后才发布；事务内的新增节点、检查点与临时替代状态不可外泄。
                    for snapshot in pending.values():
                        self.structure_cache.publish(snapshot, epoch)
                    for memory in assistant_pending.values():
                        self.assistant_memory_cache.publish(memory, assistant_epoch)
                except BaseException:
                    for owner, tree_id in assistant_pending:
                        self.assistant_memory_cache.discard(owner, tree_id)
                    for owner, tree_id in pending:
                        self.structure_cache.discard(owner, tree_id)
                    # 取消同样回滚；释放连接前不能留下半份节点、正文或工作区。
                    await connection.rollback()
                    raise
                finally:
                    self._snapshot_transactions.pop(cursor_id, None)
                    self._assistant_transactions.pop(cursor_id, None)

    async def _owned_tree(self, cursor, tree_id, owner_userid, *, lock=False):
        await cursor.execute('SELECT * FROM question_trees WHERE id=%s AND owner_userid=%s'
                             + (' FOR UPDATE' if lock else ''), (tree_id, owner_userid))
        row = await cursor.fetchone()
        if row is None:
            self.structure_cache.discard(owner_userid, tree_id)
            self.assistant_memory_cache.discard(owner_userid, tree_id)
            raise QuestionTreeNotFound('问答树不存在')
        return _row(QuestionTree, row)

    async def _snapshot(self, cursor, tree):
        if tree.kind == 'exploration':
            pending = self._snapshot_transactions.get(id(cursor), {})
            snapshot = pending.get((tree.owner_userid, tree.id))
            if snapshot is None or snapshot.tree != tree:
                snapshot = self.structure_cache.get(tree)
            if snapshot is not None:
                self._stage_snapshot(cursor, snapshot)
                return snapshot
        await cursor.execute('SELECT * FROM question_nodes WHERE tree_id=%s ORDER BY sequence', (tree.id,))
        snapshot = QuestionTreeSnapshot(tree, [_row(QuestionNode, row) for row in await cursor.fetchall()])
        self._stage_snapshot(cursor, snapshot)
        return snapshot

    def _stage_assistant_memory(self, cursor, memory):
        pending = self._assistant_transactions.get(id(cursor))
        if pending is not None:
            pending[(memory.tree.owner_userid, memory.tree.id)] = memory

    def _stage_snapshot(self, cursor, snapshot):
        pending = self._snapshot_transactions.get(id(cursor))
        if pending is not None and snapshot.tree.kind == 'exploration':
            pending[(snapshot.tree.owner_userid, snapshot.tree.id)] = snapshot

    def _update_snapshot(self, cursor, snapshot, tree, *nodes):
        """用已校验的事务写入结果更新结构，不重新查询历史节点；旧快照保持不可变。"""
        changed = {node.id: node for node in nodes}
        updated = QuestionTreeSnapshot(tree, [changed.get(node.id, node) for node in snapshot.nodes])
        self._stage_snapshot(cursor, updated)
        return updated

    async def create_tree(self, owner_userid: str, name: str = '新的问答树', *,
                          kind: TreeKind = 'exploration') -> QuestionTree:
        tree = QuestionTree(owner_userid=owner_userid, name=name, kind=kind)
        async with self._transaction() as cursor:
            await cursor.execute('INSERT INTO question_trees '
                '(id,owner_userid,name,kind,main_leaf_id,revision,created_at,updated_at) VALUES(%s,%s,%s,%s,NULL,0,%s,%s)',
                (tree.id, tree.owner_userid, tree.name, tree.kind, _date(tree.created_at), _date(tree.updated_at)))
            if tree.kind == 'assistant':
                from app.repository.assistant_memory import create_memory
                self._stage_assistant_memory(cursor, await create_memory(cursor, tree))
            else:
                self._stage_snapshot(cursor, QuestionTreeSnapshot(tree, ()))
        return tree

    async def list_trees(self, owner_userid: str, *, limit: int = 50, offset: int = 0,
                         kind: TreeKind | None = None) -> list[QuestionTree]:
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError('列表分页参数不合法')
        if kind is not None:
            kind = TypeAdapter(TreeKind).validate_python(kind)
        async with self._transaction() as cursor:
            clause = '' if kind is None else ' AND kind=%s'
            args = (owner_userid, limit, offset) if kind is None else (owner_userid, kind, limit, offset)
            await cursor.execute('SELECT * FROM question_trees WHERE owner_userid=%s'+clause+
                ' ORDER BY updated_at DESC,id DESC LIMIT %s OFFSET %s', args)
            return [_row(QuestionTree, row) for row in await cursor.fetchall()]

    async def list_exploration_trees(self, owner_userid: str) -> list[QuestionTreeListEntry]:
        """只读当前用户的探索树名称及更新时间；不加载节点、不截断为默认分页的前 50 项。"""
        async with self._transaction() as cursor:
            await cursor.execute(
                'SELECT id,name,updated_at FROM question_trees WHERE owner_userid=%s AND kind=%s '
                'ORDER BY updated_at DESC,id DESC', (owner_userid, 'exploration'))
            return [_row(QuestionTreeListEntry, row) for row in await cursor.fetchall()]

    async def rename_tree(self, tree_id: str, owner_userid: str, name: str, *, expected_revision: int) -> QuestionTree:
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid, lock=True)
            self._revision(tree, expected_revision)
            updated = QuestionTree.model_validate({**tree.model_dump(), 'name': name,
                'revision': tree.revision + 1, 'updated_at': utc_now()})
            await cursor.execute('UPDATE question_trees SET name=%s,revision=%s,updated_at=%s WHERE id=%s',
                (updated.name, updated.revision, _date(updated.updated_at), tree.id))
            snapshot = self.structure_cache.get(tree)
            if snapshot is not None:
                self._update_snapshot(cursor, snapshot, updated)
            return updated

    async def rename_exploration_tree(self, tree_id: str, owner_userid: str, name: str) -> QuestionTreeListItem:
        """在同一行锁事务中校验归属、用途并改名；列表调用方不需要持有节点版本。"""
        result = QuestionTreeListItem(id=tree_id, name=name)
        if not name.strip():
            raise ValueError('问答树名称不能为空白')
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid, lock=True)
            if tree.kind != 'exploration':
                raise QuestionTreeNotFound('问答树不存在')
            # 重复提交相同名称不改变活动时间或版本；并发改名以取得行锁的顺序生效。
            if tree.name != name:
                now = utc_now()
                await cursor.execute('UPDATE question_trees SET name=%s,revision=%s,updated_at=%s WHERE id=%s',
                    (name, tree.revision + 1, _date(now), tree.id))
                snapshot = self.structure_cache.get(tree)
                if snapshot is not None:
                    self._update_snapshot(cursor, snapshot, tree.model_copy(update={
                        'name': name, 'revision': tree.revision + 1, 'updated_at': now}))
        return result

    @staticmethod
    def _revision(tree, expected):
        if type(expected) is not int or tree.revision != expected:
            raise QuestionTreeConflict('问答树版本已变化')

    async def load_tree(self, tree_id: str, owner_userid: str) -> QuestionTreeSnapshot:
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid)
            return await self._snapshot(cursor, tree)

    async def load_exploration_tree(self, tree_id: str, owner_userid: str) -> LoadedQuestionTree:
        """完整打开本人探索树；复用同版本结构，正文与消息在同一事务批量读取。"""
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid)
            if tree.kind != 'exploration':
                raise QuestionTreeNotFound('问答树不存在')
            snapshot = await self._snapshot(cursor, tree)
            await cursor.execute('SELECT c.node_id,c.content,c.display_history FROM question_node_contents AS c '
                'JOIN question_nodes AS n ON n.id=c.node_id WHERE n.tree_id=%s', (tree_id,))
            rows = await cursor.fetchall()
            contents = {row['node_id']: _decode(row['content']) for row in rows}
            histories = {row['node_id']: StoredDisplayHistory.model_validate(_decode(row['display_history'])).validate_owner(row['node_id'])
                         for row in rows if row.get('display_history') is not None}
            if set(contents) != set(snapshot.nodes_by_id):
                raise ValueError('问答节点正文缺失或跨树')
            writes = tuple(NodeWrite(node=node, content=contents[node.id]) for node in snapshot.nodes)
            # 顺序遍历维护每条父链的有效上下文起点，校验全部摘要且避免逐摘要回溯深树。
            starts = {}
            for write in writes:
                node = write.node
                start = starts[node.parent_id] if node.parent_id is not None else node.id
                if isinstance(write.content, SummaryContent):
                    if write.content.covered_from_id != start:
                        raise ValueError('摘要覆盖起点与祖先路径不一致')
                    start = node.id
                starts[node.id] = start
            await cursor.execute('SELECT m.* FROM question_node_messages AS m '
                'JOIN question_nodes AS n ON n.id=m.node_id WHERE n.tree_id=%s ORDER BY n.sequence,m.sequence',
                (tree_id,))
            messages = tuple(_row(NodeMessage, row) for row in await cursor.fetchall())
            seen = set()
            for message in messages:
                node = snapshot.nodes_by_id.get(message.node_id)
                key = (message.node_id, message.sequence)
                if node is None or node.kind != 'task' or message.sequence is None or key in seen:
                    raise ValueError('公开消息归属或序号不合法')
                seen.add(key)
            return LoadedQuestionTree(snapshot, writes, messages, histories)

    async def save_display_history(self, tree_id, owner_userid, node_id, history):
        """展示记录先提交再发布；不推进模型检查点 revision，也不覆盖 content。"""
        history = StoredDisplayHistory.model_validate(history.model_dump()).validate_owner(node_id)
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid, lock=True)
            if tree.kind != 'exploration':
                raise QuestionTreeNotFound('问答树不存在')
            await cursor.execute('SELECT c.display_history,n.kind FROM question_node_contents AS c '
                'JOIN question_nodes AS n ON n.id=c.node_id WHERE n.tree_id=%s AND n.id=%s FOR UPDATE',
                (tree_id, node_id))
            row = await cursor.fetchone()
            if row is None or row['kind'] != 'task':
                raise QuestionTreeNotFound('任务节点不存在')
            if row['display_history'] is not None:
                old = StoredDisplayHistory.model_validate(_decode(row['display_history'])).validate_owner(node_id)
                # 重复提交同一快照幂等；过期写入不能删除已经确认的展示过程。
                if history == old:
                    return
                terminal = any(e.event == 'turn.status' and e.data.get('status') in
                    ('completed', 'cancelled', 'superseded', 'failed', 'expired') for e in old.events)
                if (terminal or history.last_sequence <= old.last_sequence or
                        history.events[:len(old.events)] != old.events):
                    raise QuestionTreeConflict('展示历史不能回退或改写')
            await cursor.execute('UPDATE question_node_contents SET display_history=%s WHERE node_id=%s',
                (_dump(history.model_dump(mode='json')), node_id))

    async def _workspace(self, cursor, tree_id, version_id):
        if version_id is None:
            return None
        await cursor.execute('SELECT * FROM question_workspaces WHERE tree_id=%s AND id=%s', (tree_id, version_id))
        row = await cursor.fetchone()
        if row is None:
            raise ValueError('工作区版本不存在或不属于本树')
        row['content'] = _decode(row['content'])
        return _row(WorkspaceVersion, row)

    async def _insert_workspaces(self, cursor, tree_id, versions):
        for version in versions:
            if version.tree_id != tree_id:
                raise ValueError('工作区版本不能跨树')
            await cursor.execute('INSERT INTO question_workspaces(id,tree_id,content,created_at) VALUES(%s,%s,%s,%s)',
                (version.id, tree_id, _dump(version.content), _date(version.created_at)))

    async def append_path(self, tree_id: str, owner_userid: str, writes: list[NodeWrite], *,
                          expected_revision: int, advance_main: bool = False,
                          workspaces: tuple[WorkspaceVersion, ...] = ()) -> QuestionTreeSnapshot:
        """原子追加一条路径，可提交 S2→E；从 D 另开路径时绝不改动 S2 或 E。"""
        if not writes or writes[-1].node.kind != 'task':
            raise ValueError('追加路径必须以任务节点结束')
        # 验证并深拷贝调用方持有的嵌套 JSON，避免等待数据库期间被外部改写。
        writes = [NodeWrite.model_validate(write.model_dump()) for write in writes]
        workspaces = tuple(WorkspaceVersion.model_validate(value.model_dump()) for value in workspaces)
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid, lock=True)
            self._revision(tree, expected_revision)
            if tree.kind=='assistant':
                raise ValueError('助理必须通过线性任务入口追加，不能保存历史工作区快照')
            return await self._append_path_locked(cursor, tree, writes,
                advance_main=advance_main, workspaces=workspaces)

    async def _append_path_locked(self, cursor, tree, writes, *, advance_main=False, workspaces=()):
        """节点执行仓库复用同一事务；调用方须先锁树行并校验版本。"""
        tree_id = tree.id
        snapshot = await self._snapshot(cursor, tree)
        if not snapshot.nodes and not advance_main:
            raise ValueError('首条路径必须建立主干')
        if advance_main and writes[0].node.parent_id != tree.main_leaf_id:
            raise ValueError('主干只能从原主干末尾延伸')
        nodes = list(snapshot.nodes)
        known = dict(snapshot.nodes_by_id)
        new_versions = {value.id for value in workspaces}
        next_sequence = nodes[-1].sequence + 1 if nodes else 1
        for index, write in enumerate(writes):
            # 归属统一由持锁仓库计算；调用方不能将历史分支伪装成主干。
            node = write.node.model_copy(update={'path_type': 'main' if advance_main else 'branch'})
            write = NodeWrite(node=node, content=write.content)
            writes[index] = write
            if node.tree_id != tree_id or node.id in known or node.sequence != next_sequence + index:
                raise ValueError('追加节点跨树、重复或序号不连续')
            if index and node.parent_id != writes[index - 1].node.id:
                raise ValueError('一次追加的节点必须属于同一条连续路径')
            parent = known.get(node.parent_id)
            if node.parent_id is not None and parent is None:
                raise ValueError('父节点不存在')
            if isinstance(write.content, SummaryContent):
                # 起点必须在摘要父节点的祖先链上，且不得跨过已经存在的最近摘要。
                # 追加尚未提交时主干指针仍指向旧末端，不能用半份路径构造最终快照。
                start = parent
                while start is not None and start.kind != 'summary' and start.parent_id is not None:
                    start = known[start.parent_id]
                if start is None or write.content.covered_from_id != start.id:
                    raise ValueError('独立摘要必须覆盖当前有效上下文的完整前缀')
            # 未提供新工作区时继承直接父节点版本；不会读取另一分支最新版本。
            if node.workspace_version_id is None and parent is not None:
                node = node.model_copy(update={'workspace_version_id': parent.workspace_version_id})
                write = NodeWrite(node=node, content=write.content)
                writes[index] = write
            if (node.workspace_version_id is not None and node.workspace_version_id not in new_versions
                    and (parent is None or node.workspace_version_id != parent.workspace_version_id)):
                raise ValueError('工作区只能继承父节点版本或使用本次新建版本')
            known[node.id] = node
            nodes.append(node)
        updated = tree.model_copy(update={
            'main_leaf_id': writes[-1].node.id if advance_main else tree.main_leaf_id,
            'revision': tree.revision + 1, 'updated_at': utc_now()})
        candidate = QuestionTreeSnapshot(updated, nodes)
        await self._insert_workspaces(cursor, tree_id, workspaces)
        for write in writes:
            node = write.node
            # 新版本在本事务内可读；跨树/不存在引用不能进入节点快照。
            await self._workspace(cursor, tree_id, node.workspace_version_id)
            await cursor.execute('INSERT INTO question_nodes '
                '(id,tree_id,parent_id,kind,path_type,status,sequence,workspace_version_id,created_at,finished_at,superseded_by_node_id) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (node.id, tree_id, node.parent_id, node.kind, node.path_type, node.status, node.sequence,
                 node.workspace_version_id, _date(node.created_at),
                 _date(node.finished_at) if node.finished_at else None,node.superseded_by_node_id))
            await cursor.execute('INSERT INTO question_node_contents(node_id,content) VALUES(%s,%s)',
                (node.id, _dump(write.content.model_dump(mode='json'))))
        await cursor.execute('UPDATE question_trees SET main_leaf_id=%s,revision=%s,updated_at=%s WHERE id=%s',
            (updated.main_leaf_id, updated.revision, _date(updated.updated_at), tree_id))
        self._stage_snapshot(cursor, candidate)
        return candidate
    async def finish_task(self, tree_id: str, owner_userid: str, node_id: str, *,
                          status: str, content: TaskContent, expected_revision: int,
                          workspace: WorkspaceVersion | None = None) -> QuestionNode:
        """只允许生成中任务一次转为终态；已结束节点与其工作区版本保持不变。"""
        content = TaskContent.model_validate(content.model_dump())
        if workspace is not None:
            workspace = WorkspaceVersion.model_validate(workspace.model_dump())
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid, lock=True)
            self._revision(tree, expected_revision)
            if tree.kind=='assistant':
                raise ValueError('助理必须通过节点检查点更新唯一工作区')
            snapshot = await self._snapshot(cursor, tree)
            node = snapshot.nodes_by_id.get(node_id)
            if node is None or node.kind != 'task' or node.status != 'generating' or status not in ('complete','cancelled','failed'):
                raise ValueError('只能结束本树中尚在生成的任务')
            await cursor.execute('SELECT content FROM question_node_contents WHERE node_id=%s', (node_id,))
            original = _CONTENT.validate_python(_decode((await cursor.fetchone())['content']))
            if not isinstance(original, TaskContent) or original.question != content.question:
                raise ValueError('结束任务不能改写原问题')
            if workspace is not None:
                await self._insert_workspaces(cursor, tree_id, (workspace,))
            updated = QuestionNode.model_validate({**node.model_dump(), 'status': status,'finished_at':utc_now(),
                'workspace_version_id': workspace.id if workspace else node.workspace_version_id})
            await cursor.execute('UPDATE question_node_contents SET content=%s WHERE node_id=%s',
                (_dump(content.model_dump(mode='json')), node_id))
            await cursor.execute('UPDATE question_nodes SET status=%s,workspace_version_id=%s,finished_at=%s WHERE id=%s',
                (updated.status, updated.workspace_version_id, _date(updated.finished_at), node_id))
            now = utc_now()
            await cursor.execute('UPDATE question_trees SET revision=revision+1,updated_at=%s WHERE id=%s',
                (_date(now), tree_id))
            self._update_snapshot(cursor, snapshot, tree.model_copy(update={
                'revision': tree.revision + 1, 'updated_at': now}), updated)
            return updated

    async def load_context(self, tree_id: str, owner_userid: str, node_id: str
                           ) -> tuple[ContextPath, tuple[NodeWrite, ...], WorkspaceVersion | None]:
        """同一一致性事务内读结构、所选路径正文和所选节点工作区；不组装模型消息。"""
        async with self._transaction() as cursor:
            tree = await self._owned_tree(cursor, tree_id, owner_userid)
            snapshot = await self._snapshot(cursor, tree)
            path = snapshot.context_path(node_id)
            ids = tuple(node.id for node in path.nodes)
            placeholders = ','.join('%s' for _ in ids)
            await cursor.execute(f'SELECT node_id,content FROM question_node_contents WHERE node_id IN ({placeholders})', ids)
            contents = {row['node_id']: _decode(row['content']) for row in await cursor.fetchall()}
            if set(contents) != set(ids):
                raise ValueError('问答节点正文缺失')
            writes = tuple(NodeWrite(node=node, content=contents[node.id]) for node in path.nodes)
            if path.summary is not None:
                summary = writes[0].content
                source = snapshot.context_path(path.summary.parent_id)
                if summary.covered_from_id != source.nodes[0].id:
                    raise ValueError('摘要覆盖起点与其祖先路径不一致')
            workspace = await self._workspace(cursor, tree_id, path.workspace_version_id)
            return path, writes, workspace

    async def close(self):
        async with self._lock:
            self._closed = True
            self.structure_cache.clear()
            self.assistant_memory_cache.clear()
            if self._pool is not None:
                self._pool.close()
                await self._pool.wait_closed()
                self._pool = None

    @asynccontextmanager
    async def memory_index_write_scope(self, tree_id, owner_userid):
        """索引发布与整树删除共用数据库树行锁，避免旧快照在删除后重新写入缓存。"""
        async with self._transaction() as cursor:
            await self._owned_tree(cursor, tree_id, owner_userid, lock=True)
            yield

    async def _task_for_memory(self, cursor, tree_id, owner_userid, node_id):
        await self._owned_tree(cursor, tree_id, owner_userid)
        await cursor.execute("SELECT id FROM question_nodes WHERE tree_id=%s AND id=%s AND kind='task'",
                             (tree_id, node_id))
        if await cursor.fetchone() is None:
            raise ValueError('记忆来源必须是本树任务节点')

    @staticmethod
    def _memory_row(row):
        values = dict(row)
        values['embedding'] = decode_embedding(values['embedding'])
        return _row(QuestionNodeMemory, values)

    async def register_memory(self, tree_id: str, owner_userid: str, *, node_id: str,
                              source_id: str, kind: str) -> QuestionNodeMemory:
        """同节点、类型、来源只登记一次；重复请求不清空已完成向量。"""
        memory = QuestionNodeMemory(node_id=node_id, source_id=source_id, kind=kind)
        async with self._transaction() as cursor:
            await self._task_for_memory(cursor, tree_id, owner_userid, node_id)
            await cursor.execute('INSERT INTO question_node_memories '
                '(id,node_id,source_id,kind,embedding_status,created_at) VALUES(%s,%s,%s,%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE id=question_node_memories.id',
                (memory.id, node_id, source_id, kind, 'pending', _date(memory.created_at)))
            # 写后当前读，避免并发幂等登记在 REPEATABLE READ 快照中看不到赢家。
            await cursor.execute('SELECT * FROM question_node_memories '
                'WHERE node_id=%s AND kind=%s AND source_id=%s FOR UPDATE', (node_id, kind, source_id))
            return self._memory_row(await cursor.fetchone())

    async def list_node_memories(self, tree_id: str, owner_userid: str, node_id: str) -> list[QuestionNodeMemory]:
        async with self._transaction() as cursor:
            await self._task_for_memory(cursor, tree_id, owner_userid, node_id)
            await cursor.execute('SELECT * FROM question_node_memories WHERE node_id=%s '
                'ORDER BY created_at,id', (node_id,))
            return [self._memory_row(row) for row in await cursor.fetchall()]

    async def list_searchable_memories(self, tree_id: str, owner_userid: str) -> list[QuestionNodeMemory]:
        """只读取本人指定树中成功完成的记忆；失败不能作为空快照刷新索引。"""
        async with self._transaction() as cursor:
            await self._owned_tree(cursor, tree_id, owner_userid)
            await cursor.execute('SELECT m.* FROM question_node_memories AS m '
                'JOIN question_nodes AS n ON n.id=m.node_id '
                "WHERE n.tree_id=%s AND n.kind='task' AND m.embedding_status='success' "
                "AND (n.status<>'cancelled' OR m.kind='question') ORDER BY m.id", (tree_id,))
            return [self._memory_row(row) for row in await cursor.fetchall()]

    async def load_searchable_memory_documents(self, tree_id: str, owner_userid: str,
                                                memory_ids: list[str] | None = None) -> list[SearchableMemory]:
        """完整刷新或候选回查；正文只从节点、类型和来源均一致的公开消息取得。"""
        if memory_ids is not None:
            if len(memory_ids) > 100 or len(set(memory_ids)) != len(memory_ids):
                raise ValueError('记忆候选数量或唯一性不合法')
            for memory_id in memory_ids:
                TypeAdapter(TreeID).validate_python(memory_id)
        async with self._transaction() as cursor:
            await self._owned_tree(cursor, tree_id, owner_userid)
            if memory_ids == []:
                return []
            clause = '' if memory_ids is None else ' AND m.id IN ('+','.join(['%s']*len(memory_ids))+')'
            await cursor.execute('SELECT m.*,s.text AS source_text FROM question_node_memories AS m '
                'JOIN question_nodes AS n ON n.id=m.node_id '
                "LEFT JOIN question_node_messages AS s ON s.id=m.source_id AND s.node_id=m.node_id AND s.kind=m.kind AND s.status='completed' "
                "WHERE n.tree_id=%s AND n.kind='task' AND m.embedding_status='success' "
                "AND (n.status<>'cancelled' OR m.kind='question')"+clause+' ORDER BY m.id',
                [tree_id,*(memory_ids or [])])
            documents=[]
            for row in await cursor.fetchall():
                values=dict(row)
                # 旧接口允许自定义来源 ID；无规范化消息时仍可向量召回，但不伪造全文正文。
                text=values.pop('source_text') or ''
                documents.append(SearchableMemory(memory=self._memory_row(values),text=text))
            return documents

    async def _locked_memory(self, cursor, tree_id, owner_userid, memory_id):
        await self._owned_tree(cursor, tree_id, owner_userid)
        await cursor.execute('SELECT m.* FROM question_node_memories AS m '
            'JOIN question_nodes AS n ON n.id=m.node_id WHERE n.tree_id=%s AND m.id=%s FOR UPDATE',
            (tree_id, memory_id))
        row = await cursor.fetchone()
        if row is None:
            raise ValueError('节点记忆不存在')
        return self._memory_row(row)

    def _embedding_job(self, row):
        values = dict(row)
        tree_id, owner, text = values.pop('tree_id'), values.pop('owner_userid'), values.pop('source_text')
        return MemoryEmbeddingJob(tree_id=tree_id, owner_userid=owner,
            memory=self._memory_row(values), text=text)

    async def list_memory_embedding_jobs(self, *, after_id=None, limit=32):
        """仅供可信后台扫描；使用有界游标分页，不接受模型或 HTTP 指定查询范围。"""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError('记忆扫描批量必须为 1 至 256 的整数')
        if after_id is not None:
            TypeAdapter(TreeID).validate_python(after_id)
        clause = ' AND m.id>%s' if after_id is not None else ''
        async with self._transaction() as cursor:
            await cursor.execute(_EMBEDDING_SOURCE + clause + ' ORDER BY m.id LIMIT %s',
                (after_id, limit) if after_id is not None else (limit,))
            return [self._embedding_job(row) for row in await cursor.fetchall()]

    async def store_memory_embedding(self, job: MemoryEmbeddingJob, vector):
        """在同一树锁下复核来源后落盘；删除、失效、成功记录及过期快照不写入。"""
        job = MemoryEmbeddingJob.model_validate(job)
        encoded = None if vector is None else encode_embedding(normalize_embedding(vector))
        async with self._transaction() as cursor:
            try:
                await self._owned_tree(cursor, job.tree_id, job.owner_userid, lock=True)
            except QuestionTreeNotFound:
                return False
            await cursor.execute(_EMBEDDING_SOURCE + ' AND m.id=%s AND n.tree_id=%s FOR UPDATE',
                (job.memory.id, job.tree_id))
            row = await cursor.fetchone()
            if row is None:
                return False
            current = self._embedding_job(row)
            if (current.text != job.text or current.memory.node_id != job.memory.node_id
                    or current.memory.source_id != job.memory.source_id or current.memory.kind != job.memory.kind):
                return False
            await cursor.execute('UPDATE question_node_memories SET embedding=%s,embedding_status=%s,embedded_at=%s WHERE id=%s',
                (encoded, 'success' if encoded is not None else 'failed',
                 _date(utc_now()) if encoded is not None else None, job.memory.id))
            # 不更新树 revision、工作区或用户消息；成功结果后续扫描自然跳过。
            return True

    async def complete_memory(self, tree_id: str, owner_userid: str, memory_id: str,
                              embedding: list[float] | tuple[float, ...]) -> QuestionNodeMemory:
        vector = normalize_embedding(embedding)
        async with self._transaction() as cursor:
            memory = await self._locked_memory(cursor, tree_id, owner_userid, memory_id)
            if memory.embedding_status == 'success':
                if memory.embedding != vector:
                    raise QuestionTreeConflict('已完成的记忆向量不能被其他结果覆盖')
                return memory
            updated = QuestionNodeMemory.model_validate({**memory.model_dump(), 'embedding': vector,
                'embedding_status': 'success', 'embedded_at': utc_now()})
            await cursor.execute("UPDATE question_node_memories SET embedding=%s,embedding_status='success',embedded_at=%s WHERE id=%s",
                (encode_embedding(updated.embedding), _date(updated.embedded_at), memory_id))
            return updated

    async def fail_memory(self, tree_id: str, owner_userid: str, memory_id: str) -> QuestionNodeMemory:
        async with self._transaction() as cursor:
            memory = await self._locked_memory(cursor, tree_id, owner_userid, memory_id)
            if memory.embedding_status == 'success':
                raise QuestionTreeConflict('已完成的记忆不能降级为失败')
            await cursor.execute("UPDATE question_node_memories SET embedding_status='failed' WHERE id=%s", (memory_id,))
            return memory.model_copy(update={'embedding_status': 'failed'})
