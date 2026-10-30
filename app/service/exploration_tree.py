"""探索树打开、驻留与删除；数据库授权和事务仍为权威。"""

from contextlib import asynccontextmanager
import asyncio

from app.infrastructure.upload_file import UploadFileStore
from app.model.question_tree import TaskContent
from app.repository.question_tree import MySQLQuestionTreeRepository, QuestionTreeNotFound
from app.repository.question_tree_deletion import QuestionTreeDeletionRepository
from app.model.display_history import DisplayEvent, TaskDisplayHistory
from app.schema.exploration import ExplorationNodeResponse, ExplorationTreeResponse, MessageDisplay, TaskDisplayContent
from app.service.question_tree import LoadedQuestionTree, QuestionTreeSnapshot
from app.agent.core.subgraph.context_management.topic_summary.rendering import display_text


def render_tree(loaded: LoadedQuestionTree) -> ExplorationTreeResponse:
    snapshot = loaded.snapshot
    messages = {}
    for message in loaded.messages:
        messages.setdefault(message.node_id, []).append(MessageDisplay.model_validate(
            message.model_dump(exclude={'node_id'})))
    nodes = []
    for write in loaded.contents:
        node, content = write.node, write.content
        if isinstance(content, TaskContent):
            content = TaskDisplayContent(question=content.question, attachments=tuple(content.attachments), answer=content.answer)
        else:
            # 落盘保留主题来源供下次整理，公开树仍只返回可读摘要，不暴露编码结构。
            content = content.model_copy(update={'text': display_text(content.text)})
        display = None
        if node.kind == 'task':
            recorded = loaded.display_histories.get(node.id)
            if recorded is not None:
                display = TaskDisplayHistory(**recorded.model_dump(), event_source='recorded')
            else:
                # 旧节点没有实际事件日志；只恢复已有输出，不臆造动作、错误或 SSE 游标。
                display = TaskDisplayHistory(event_source='legacy', events=tuple(
                    DisplayEvent(event='message.completed', data={'turn_id': node.id,
                        'message_id': m.id, 'message_kind': m.kind, 'text': m.text, 'status': m.status})
                    for m in messages.get(node.id, ()) if m.kind != 'question'))
        nodes.append(ExplorationNodeResponse(
            **node.model_dump(exclude={'tree_id', 'workspace_version_id'}),
            children_ids=tuple(child.id for child in snapshot.children_by_parent.get(node.id, ())),
            context_status=node.context_status, can_interrupt=node.can_interrupt,
            processing_duration_ms=node.processing_duration_ms,
            content=content, messages=tuple(messages.get(node.id, ())), display=display))
    roots = snapshot.children_by_parent.get(None, ())
    return ExplorationTreeResponse(**snapshot.tree.model_dump(exclude={'owner_userid'}),
        root_id=roots[0].id if roots else None, nodes=tuple(nodes))


class ExplorationTreeService:
    def __init__(self, repository: MySQLQuestionTreeRepository, *, max_loaded_trees: int = 32,
                 upload_files: UploadFileStore | None = None):
        if type(max_loaded_trees) is not int or max_loaded_trees < 1:
            raise ValueError('驻留树容量必须为正整数')
        self._repository = repository
        self._deletions = QuestionTreeDeletionRepository(repository)
        self._files = upload_files if upload_files is not None else UploadFileStore()
        # 打开、注册与生成共用仓库的结构池，避免展示层持有另一份从未参与执行的树。
        self._structures = repository.structure_cache
        self._structures.resize(max_loaded_trees)
        self._closed = False
        self._operations = {}
        self.turns = None

    @asynccontextmanager
    async def _operation(self, key):
        # 同一进程的打开/删除串行，防止已删除的树被晚到打开结果重新驻留；不同树互不等待。
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

    def operation(self, tree_id, owner_userid):
        """同树注册、激活、打开及删除共用协调锁；模型执行不持锁。"""
        return self._operation((owner_userid, tree_id))

    async def open_tree(self, tree_id: str, owner_userid: str) -> ExplorationTreeResponse:
        async with self._operation((owner_userid, tree_id)):
            return await self._open_tree(tree_id, owner_userid)

    async def _open_tree(self, tree_id: str, owner_userid: str) -> ExplorationTreeResponse:
        if self._closed:
            raise RuntimeError('探索树服务已关闭')
        key = (owner_userid, tree_id)
        try:
            # 每次打开仍检查权威树行；结构同版本命中时复用，展示正文从当前事务读取。
            loaded = await self._repository.load_exploration_tree(tree_id, owner_userid)
            tree = loaded.snapshot.tree
            if tree.id != tree_id or tree.owner_userid != owner_userid or tree.kind != 'exploration':
                raise QuestionTreeNotFound('问答树不存在')
            response = render_tree(loaded)
        except BaseException:
            self._structures.discard(*key)
            raise
        if self._closed:
            raise RuntimeError('探索树服务已关闭')
        return response

    async def delete_tree(self, tree_id: str, owner_userid: str) -> None:
        async with self._operation((owner_userid, tree_id)):
            try:
                if self.turns is not None:
                    await self.turns.remove_tree(tree_id, owner_userid)
                attachments = await self._deletions.delete(tree_id, owner_userid)
                self._structures.discard(owner_userid, tree_id)
                for reference in attachments:
                    await self._files.delete(reference)
                await self._deletions.finish(tree_id, owner_userid)
            finally:
                # 提交回执不确定或文件清理失败也不得继续使用旧快照。
                self._structures.discard(owner_userid, tree_id)

    def get_loaded_snapshot(self, tree_id: str, owner_userid: str) -> QuestionTreeSnapshot | None:
        """内部结构索引访问；调用方仍须认证，后续写入必须重新检查数据库版本。"""
        return self._structures.peek(owner_userid, tree_id)

    async def close(self):
        self._closed = True
        self._structures.clear()
