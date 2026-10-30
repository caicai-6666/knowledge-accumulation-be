"""助理有效记忆窗口及单份工作区；压缩结果和覆盖边界在检查点事务内一起更新。"""
from app.model.question_tree import NodeWrite, QuestionNode, WorkspaceVersion, utc_now
from app.model.node_persistence import NodeMessage
from app.repository.question_tree import _date, _dump, _decode, _row
from app.service.question_tree import ContextPath
from app.service.assistant_memory import AssistantMemory


def resident_write(write):
    # 压缩检查点已投影到唯一 summary；原始检查点保留在 SQL 审计，不能在驻留窗口重复应用。
    content=write.content.model_copy(update={'exchanges':[entry for entry in write.content.exchanges
        if entry.get('kind')!='context_summary']})
    return write.model_copy(update={'content':content},deep=True)


def make_memory(tree, writes, messages, workspace, summary=None, first_sequence=1):
    writes=tuple(resident_write(write) for write in writes)
    nodes=tuple(write.node for write in writes)
    if nodes:
        if nodes[-1].id!=tree.main_leaf_id:
            raise ValueError('助理驻留末端与主干不一致')
        for index,node in enumerate(nodes):
            if node.kind!='task' or node.path_type!='main' or node.workspace_version_id is not None:
                raise ValueError('助理节点必须线性追加且不保存工作区历史引用')
            if index and (node.parent_id!=nodes[index-1].id or node.sequence<=nodes[index-1].sequence
                          or nodes[index-1].status=='generating'):
                raise ValueError('助理驻留父链不连续或包含未完成祖先')
    elif tree.main_leaf_id is not None:
        raise ValueError('助理有效记忆窗口缺失末端')
    ids={node.id for node in nodes}
    path=ContextPath(None,nodes,workspace.id,summary) if nodes else None
    return AssistantMemory(tree,path,writes,tuple(m for m in messages if m.node_id in ids),workspace,summary,first_sequence)


async def create_memory(cursor,tree):
    workspace=WorkspaceVersion(tree_id=tree.id,content={})
    await cursor.execute('INSERT INTO assistant_workspaces(tree_id,id,content,summary,first_sequence,created_at,updated_at) '
        'VALUES(%s,%s,%s,NULL,1,%s,%s)',(tree.id,workspace.id,'{}',_date(workspace.created_at),_date(workspace.created_at)))
    return make_memory(tree,(),(),workspace)


async def current_workspace(cursor,tree):
    await cursor.execute('SELECT id,tree_id,content,created_at FROM assistant_workspaces WHERE tree_id=%s',(tree.id,))
    row=await cursor.fetchone()
    if row is None:raise ValueError('助理当前工作区未初始化，请先完成 021 迁移')
    row['content']=_decode(row['content'])
    return _row(WorkspaceVersion,row)


async def load_memory(trees,cursor,tree):
    if tree.kind!='assistant':raise ValueError('仅允许读取个人助理驻留窗口')
    pending=trees._assistant_transactions.get(id(cursor),{})
    memory=pending.get((tree.owner_userid,tree.id)) or trees.assistant_memory_cache.get(tree)
    if memory is not None and memory.tree==tree:
        trees._stage_assistant_memory(cursor,memory)
        return memory
    await cursor.execute('SELECT * FROM assistant_workspaces WHERE tree_id=%s',(tree.id,))
    row=await cursor.fetchone()
    if row is None:raise ValueError('助理记忆窗口未初始化，请先完成 021 迁移')
    workspace=_row(WorkspaceVersion,{key:(_decode(row[key]) if key=='content' else row[key])
        for key in ('id','tree_id','content','created_at')})
    # 边界先在 SQL 下推，冷启动也不加载已被摘要覆盖的节点。
    await cursor.execute("SELECT * FROM question_nodes WHERE tree_id=%s AND kind='task' AND sequence>=%s ORDER BY sequence",
        (tree.id,row['first_sequence']))
    nodes=tuple(_row(QuestionNode,value) for value in await cursor.fetchall())
    writes=();messages=()
    if nodes:
        ids=[node.id for node in nodes];marks=','.join(['%s']*len(ids))
        await cursor.execute('SELECT node_id,content FROM question_node_contents WHERE node_id IN ('+marks+')',ids)
        contents={value['node_id']:_decode(value['content']) for value in await cursor.fetchall()}
        if set(contents)!=set(ids):raise ValueError('助理驻留节点正文缺失')
        writes=tuple(NodeWrite(node=node,content=contents[node.id]) for node in nodes)
        await cursor.execute("SELECT * FROM question_node_messages WHERE status='completed' AND node_id IN ("+marks+
            ') ORDER BY sequence,id',ids)
        messages=tuple(_row(NodeMessage,value) for value in await cursor.fetchall())
    memory=make_memory(tree,writes,messages,workspace,row['summary'],row['first_sequence'])
    trees._stage_assistant_memory(cursor,memory)
    return memory


async def commit_memory(trees,cursor,before,restored,*,new_exchanges=(),summary=None):
    """只消费本次新增的已接受压缩检查点；重放不会重复驱逐，失败事务不发布内存。"""
    current=NodeWrite(node=restored.node,content=restored.content)
    if summary is not None:
        writes=[current];text=summary.text
    else:
        writes=[current if write.node.id==current.node.id else write for write in before.writes]
        if not any(write.node.id==current.node.id for write in writes):writes.append(current)
        text=before.summary
    for entry in new_exchanges:
        if entry.get('kind')!='context_summary' or 'covered_turns' not in entry:continue
        count=entry['covered_turns']
        # 当前任务不参与前缀压缩；范围非法意味着没有任何工作区或驱逐被接受。
        if type(count) is not int or not 0<count<len(writes) or not isinstance(entry.get('text'),str) or not entry['text'].strip():
            raise ValueError('助理压缩覆盖范围不合法，不能覆盖当前执行任务')
        writes=writes[count:];text=entry['text']
    first=writes[0].node.sequence
    messages=tuple(m for m in before.messages if m.node_id!=restored.node.id)+tuple(
        m for m in restored.messages if m.status=='completed')
    memory=make_memory(restored.tree,writes,messages,restored.workspace,text,first)
    await cursor.execute('UPDATE assistant_workspaces SET summary=%s,first_sequence=%s,updated_at=%s WHERE tree_id=%s',
        (text,first,_date(utc_now()),restored.tree.id))
    trees._stage_assistant_memory(cursor,memory)
    return memory
