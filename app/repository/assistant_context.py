"""助理线性上下文：只回溯当前末端至最近摘要，不构建完整树快照。"""
from app.model.question_tree import NodeWrite, QuestionNode, WorkspaceVersion, utc_now
from app.repository.question_tree import _date, _dump, _row
from app.service.question_tree import ContextPath


async def assistant_tail(cursor, tree):
    if tree.kind != 'assistant':
        raise ValueError('仅适用于助理树')
    # 使用 (tree_id,sequence) 索引，只取末行；发现旧分支或游离末端不能悄悄选择另一节点。
    await cursor.execute('SELECT * FROM question_nodes WHERE tree_id=%s ORDER BY sequence DESC LIMIT 1', (tree.id,))
    row = await cursor.fetchone()
    if row is None:
        if tree.main_leaf_id is not None:
            raise ValueError('助理主干末端不存在')
        return None
    tail = _row(QuestionNode, row)
    if tail.id != tree.main_leaf_id or tail.kind != 'task' or tail.path_type != 'main':
        raise ValueError('助理树末端与线性主干不一致')
    if tail.status == 'generating':
        raise ValueError('助理当前任务尚未结束')
    return tail


async def assistant_path(cursor, tree):
    tail = await assistant_tail(cursor, tree)
    if tail is None:
        return None
    tasks = []
    current = tail
    while True:
        if current.path_type != 'main' or current.status == 'generating':
            raise ValueError('助理有效上下文包含分支或未完成节点')
        if current.kind == 'summary':
            # 摘要是可信的已提交边界：这里绝不读取其父节点或更早摘要。
            return ContextPath(current, tuple(reversed(tasks)), tail.workspace_version_id)
        tasks.append(current)
        if current.parent_id is None:
            return ContextPath(None, tuple(reversed(tasks)), tail.workspace_version_id)
        await cursor.execute('SELECT * FROM question_nodes WHERE tree_id=%s AND id=%s',
                             (tree.id, current.parent_id))
        row = await cursor.fetchone()
        if row is None:
            raise ValueError('助理上下文父节点缺失或跨树')
        parent = _row(QuestionNode, row)
        if parent.sequence >= current.sequence:
            raise ValueError('助理上下文父子顺序不合法')
        current = parent


async def append_assistant_task(cursor, trees, tree, node_id, content, parent_id, advance_main, summary, summary_id):
    """调用者持树行锁并已检查 revision；普通追加只读末端，压缩时才读有效父链。"""
    if parent_id != tree.main_leaf_id or advance_main is False:
        raise ValueError('助理只能从最新主干末端继续')
    if summary is not None:
        path = await assistant_path(cursor, tree)
        if path is None or summary.covered_from_id != path.nodes[0].id or summary.covered_to_id != parent_id:
            raise ValueError('助理摘要必须覆盖当前有效上下文的完整前缀')
        parent = path.nodes[-1]
    else:
        parent = await assistant_tail(cursor, tree)
    version_id = parent.workspace_version_id if parent else None
    if parent is not None and version_id is None:
        raise ValueError('助理父节点缺少工作区快照')
    workspace = None if version_id else WorkspaceVersion(tree_id=tree.id, content={})
    version_id = version_id or workspace.id
    sequence = parent.sequence + 1 if parent else 1
    writes = []
    if summary is not None:
        writes.append(NodeWrite(node=QuestionNode(id=summary_id, tree_id=tree.id, parent_id=parent_id,
            kind='summary', path_type='main', sequence=sequence, workspace_version_id=version_id), content=summary))
    writes.append(NodeWrite(node=QuestionNode(id=node_id, tree_id=tree.id,
        parent_id=summary_id if summary else parent_id, kind='task', status='generating', path_type='main',
        sequence=sequence + int(summary is not None), workspace_version_id=version_id), content=content))
    if workspace:
        await trees._insert_workspaces(cursor, tree.id, (workspace,))
    await trees._workspace(cursor, tree.id, version_id)
    for write in writes:
        node = write.node
        await cursor.execute('INSERT INTO question_nodes '
            '(id,tree_id,parent_id,kind,path_type,status,sequence,workspace_version_id,created_at,finished_at,superseded_by_node_id) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (node.id,tree.id,node.parent_id,node.kind,node.path_type,node.status,node.sequence,version_id,
             _date(node.created_at),None,None))
        await cursor.execute('INSERT INTO question_node_contents(node_id,content) VALUES(%s,%s)',
            (node.id,_dump(write.content.model_dump(mode='json'))))
    updated = tree.model_copy(update={'main_leaf_id': node_id, 'revision': tree.revision + 1, 'updated_at': utc_now()})
    await cursor.execute('UPDATE question_trees SET main_leaf_id=%s,revision=%s,updated_at=%s WHERE id=%s',
                         (updated.main_leaf_id,updated.revision,_date(updated.updated_at),tree.id))
    return updated
