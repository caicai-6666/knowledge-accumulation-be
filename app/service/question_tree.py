"""不可变的内存树快照：只索引结构，正文和工作区按需由仓库加载。"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from app.model.question_tree import NodeWrite, QuestionNode, QuestionTree
from app.model.node_persistence import NodeMessage
from app.model.display_history import StoredDisplayHistory


@dataclass(frozen=True)
class ContextPath:
    summary: QuestionNode | None
    tasks: tuple[QuestionNode, ...]
    workspace_version_id: str | None

    @property
    def nodes(self) -> tuple[QuestionNode, ...]:
        return ((self.summary,) if self.summary else ()) + self.tasks


@dataclass(frozen=True)
class LoadedQuestionTree:
    """同一事务取得的完整树结构、节点正文和公开消息；不读取原生生成或工作区正文。"""

    snapshot: 'QuestionTreeSnapshot'
    contents: tuple[NodeWrite, ...]
    messages: tuple[NodeMessage, ...]
    display_histories: dict[str, StoredDisplayHistory] = field(default_factory=dict)


class QuestionTreeSnapshot:
    def __init__(self, tree: QuestionTree, nodes: Iterable[QuestionNode]):
        ordered = tuple(nodes)
        index = {node.id: node for node in ordered}
        if len(index) != len(ordered):
            raise ValueError('问答树存在重复节点')
        children: dict[str | None, list[QuestionNode]] = {}
        sequences = set()
        for node in ordered:
            if node.tree_id != tree.id or node.sequence in sequences:
                raise ValueError('节点跨树或序号重复')
            sequences.add(node.sequence)
            if node.parent_id is not None:
                parent = index.get(node.parent_id)
                # 严格递增序号同时排除环；不会递归遍历，深树不受 Python 递归栈限制。
                if parent is None or parent.sequence >= node.sequence or parent.status == 'generating':
                    raise ValueError('父节点不存在、顺序不合法或尚在生成')
            children.setdefault(node.parent_id, []).append(node)
        if ordered and len(children.get(None, ())) != 1:
            raise ValueError('非空问答树必须有且只有一个根节点')
        if tree.main_leaf_id is not None:
            main = index.get(tree.main_leaf_id)
            if main is None or main.kind != 'task':
                raise ValueError('主干末尾必须指向本树任务节点')
        elif ordered:
            raise ValueError('非空树缺少主干末尾')
        for node in ordered:
            if node.status=='superseded':
                replacement=index.get(node.superseded_by_node_id)
                if replacement is None or replacement.kind!='task' or replacement.parent_id!=node.id:
                    raise ValueError('被替代节点必须关联本树的接替任务子节点')
        # 归属是父链的冗余标记，不能取代拓扑或把另一分支当作上下文。
        main_ids = set()
        current = index.get(tree.main_leaf_id)
        while current is not None:
            main_ids.add(current.id)
            current = index.get(current.parent_id)
        if any(node.path_type != ('main' if node.id in main_ids else 'branch') for node in ordered):
            raise ValueError('节点主干/分支标记与主干父链不一致，需先校验或回填数据')
        # SQL 按 sequence 排序时保持线性构建；其他调用方可传任意顺序。
        if any(a.sequence > b.sequence for a, b in zip(ordered, ordered[1:])):
            ordered = tuple(sorted(ordered, key=lambda n: n.sequence))
            children = {}
            for node in ordered:
                children.setdefault(node.parent_id, []).append(node)
        self.tree = tree
        self.nodes = ordered
        self.nodes_by_id: Mapping[str, QuestionNode] = MappingProxyType(index)
        self.children_by_parent: Mapping[str | None, tuple[QuestionNode, ...]] = MappingProxyType(
            {key: tuple(value) for key, value in children.items()})

    def context_path(self, node_id: str) -> ContextPath:
        """从所选节点逆向查找第一个摘要；绝不使用所选节点之后的摘要。"""
        selected = self.nodes_by_id.get(node_id)
        if selected is None:
            raise KeyError('问答节点不存在')
        if selected.status == 'generating':
            raise ValueError('生成中的节点不能作为继续提问的起点')
        current = selected
        tasks = []
        summary = None
        while current is not None:
            if current.kind == 'summary':
                summary = current
                break
            tasks.append(current)
            current = self.nodes_by_id.get(current.parent_id)
        return ContextPath(summary=summary, tasks=tuple(reversed(tasks)),
                           workspace_version_id=selected.workspace_version_id)
