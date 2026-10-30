"""个人助理驻留窗口：最新摘要、后续节点及唯一当前工作区，不缓存私有审计。"""
from dataclasses import dataclass
from app.model.question_tree import QuestionTree, NodeWrite, WorkspaceVersion
from app.model.node_persistence import NodeMessage
from app.service.question_tree import ContextPath


@dataclass(frozen=True)
class AssistantMemory:
    tree: QuestionTree
    path: ContextPath | None
    writes: tuple[NodeWrite, ...]
    messages: tuple[NodeMessage, ...]
    workspace: WorkspaceVersion
    summary: str | None = None
    first_sequence: int = 1

    @property
    def nodes(self):
        return tuple(write.node for write in self.writes)

    def ancestor_tasks(self, node_id):
        # 内存窗口不是完整树：摘要的父节点只作为磁盘历史引用，不继续解析。
        ids = [node.id for node in self.nodes]
        if node_id not in ids:
            raise ValueError('任务不在助理当前记忆范围内')
        return tuple(node for node in self.nodes[:ids.index(node_id)] if node.kind=='task')


class AssistantMemoryCache:
    def __init__(self):
        self._entries = {}
        self.epoch = 0

    def peek(self, owner, tree_id):
        return self._entries.get((owner, tree_id))

    def get(self, tree):
        value = self.peek(tree.owner_userid, tree.id)
        return value if value is not None and value.tree == tree else None

    def publish(self, memory, epoch):
        if epoch != self.epoch:return
        key = (memory.tree.owner_userid, memory.tree.id)
        old = self._entries.get(key)
        if old is None or old.tree.revision <= memory.tree.revision:
            self._entries[key] = memory

    def discard(self, owner, tree_id):
        self._entries.pop((owner, tree_id), None)
        self.epoch += 1

    def clear(self):
        self._entries.clear()
        self.epoch += 1
