"""记忆页面只回读可信父链上的公开任务；不加载私有生成或工作区。"""
from dataclasses import dataclass
from app.model.node_persistence import NodeMessage
from app.model.question_tree import NodeWrite
from app.repository.question_tree import _decode, _row


class MemoryWindowError(ValueError):
    """只传递窗口校验分支给出的安全原因和修正动作，不回显底层异常。"""

    def __init__(self, reason, required_action):
        super().__init__(reason)
        self.reason, self.required_action = reason, required_action


@dataclass(frozen=True)
class MemoryTask:
    write: NodeWrite
    messages: tuple[NodeMessage, ...]


async def read_memory_tasks(repository, tree_id, owner, current_node_id, node_ids):
    if len(node_ids) > 20 or len(set(node_ids)) != len(node_ids):
        raise ValueError('记忆任务数量或唯一性不合法')
    async with repository._transaction() as cursor:
        tree = await repository._owned_tree(cursor, tree_id, owner)
        snapshot = await repository._snapshot(cursor, tree)
        ancestors = {node.id: node for node in snapshot.ancestor_tasks(current_node_id)}
        if any(identifier not in ancestors for identifier in node_ids):
            raise ValueError('历史任务不属于当前祖先范围')
        if not node_ids:
            return ()
        marks = ','.join(['%s'] * len(node_ids))
        await cursor.execute('SELECT node_id,content FROM question_node_contents WHERE node_id IN ('+marks+')', node_ids)
        contents = {row['node_id']: _decode(row['content']) for row in await cursor.fetchall()}
        if set(contents) != set(node_ids):
            raise ValueError('历史任务正文缺失')
        await cursor.execute("SELECT * FROM question_node_messages WHERE status='completed' AND node_id IN ("+marks+
                             ') ORDER BY sequence,id', node_ids)
        messages = tuple(_row(NodeMessage, row) for row in await cursor.fetchall())
        return tuple(MemoryTask(NodeWrite(node=ancestors[i], content=contents[i]),
            tuple(m for m in messages if m.node_id == i and (ancestors[i].status != 'cancelled' or m.kind == 'question')))
            for i in node_ids)
