"""显式维护命令：将个人助理迁移为压缩窗口及单份工作区，应用启动不调用。"""
import asyncio
import json

from app.repository.question_tree import MySQLQuestionTreeRepository, _decode, _dump, _date
from app.repository.assistant_context import assistant_path
from app.model.question_tree import NodeWrite, WorkspaceVersion, utc_now


async def migrate_tree(repository,tree_id,owner):
    async with repository._transaction() as cursor:
        tree=await repository._owned_tree(cursor,tree_id,owner,lock=True)
        if tree.kind!='assistant':raise ValueError('仅迁移个人助理')
        await cursor.execute('SELECT tree_id FROM assistant_workspaces WHERE tree_id=%s',(tree_id,))
        if await cursor.fetchone() is not None:return False
        # 必须先停写；生成中任务不猜测哪个检查点可供恢复。
        path=await assistant_path(cursor,tree)
        summary=None;retained=[]
        if path is None:
            workspace=WorkspaceVersion(tree_id=tree_id,content={})
        else:
            ids=[node.id for node in path.nodes];marks=','.join(['%s']*len(ids))
            await cursor.execute('SELECT node_id,content FROM question_node_contents WHERE node_id IN ('+marks+')',ids)
            contents={row['node_id']:_decode(row['content']) for row in await cursor.fetchall()}
            if set(contents)!=set(ids):raise ValueError('迁移所需的有效历史缺失')
            for node in path.nodes:
                write=NodeWrite(node=node,content=contents[node.id])
                if node.kind=='summary':
                    summary=write.content.text;retained=[]
                    continue
                for entry in write.content.exchanges:
                    if entry.get('kind')!='context_summary' or 'covered_turns' not in entry:continue
                    count=entry['covered_turns']
                    if type(count) is not int or not 0<count<=len(retained) or not isinstance(entry.get('text'),str) or not entry['text'].strip():
                        raise ValueError('已存压缩检查点覆盖范围不合法，停止迁移')
                    summary=entry['text'];retained=retained[count:]
                retained.append(node)
            workspace=await repository._workspace(cursor,tree_id,path.workspace_version_id)
            if workspace is None:raise ValueError('最新工作区缺失，不能删除历史快照')
        first=retained[0].sequence if retained else 1
        await cursor.execute('INSERT INTO assistant_workspaces(tree_id,id,content,summary,first_sequence,created_at,updated_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s)',(tree_id,workspace.id,_dump(workspace.content),summary,first,
            _date(workspace.created_at),_date(utc_now())))
        # 新记录和覆盖范围确认在同一事务中，先解除全部节点引用再清理历史快照。
        await cursor.execute('UPDATE question_nodes SET workspace_version_id=NULL WHERE tree_id=%s',(tree_id,))
        await cursor.execute('DELETE FROM question_workspaces WHERE tree_id=%s',(tree_id,))
        await cursor.execute('UPDATE question_trees SET revision=revision+1 WHERE id=%s',(tree_id,))
        from app.repository.assistant_memory import load_memory
        await load_memory(repository,cursor,tree.model_copy(update={'revision':tree.revision+1}))
    repository.assistant_memory_cache.discard(owner,tree_id)
    return True


async def migrate_all(repository):
    async with repository._transaction() as cursor:
        await cursor.execute("SELECT id,owner_userid FROM question_trees WHERE kind='assistant' ORDER BY id")
        trees=await cursor.fetchall()
    count=0
    for tree in trees:count+=await migrate_tree(repository,tree['id'],tree['owner_userid'])
    return {'assistant_trees':len(trees),'migrated':count,'already_migrated':len(trees)-count}


async def main():
    repository=MySQLQuestionTreeRepository()
    try:print(json.dumps(await migrate_all(repository)))
    finally:await repository.close()


if __name__=='__main__':asyncio.run(main())
