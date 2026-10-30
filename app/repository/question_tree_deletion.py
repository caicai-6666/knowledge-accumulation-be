"""整树 SQL 删除与持久化文件清理清单；文件 I/O 在提交后执行。"""

from pydantic import TypeAdapter

from app.model.question_file import AttachmentReference
from app.model.question_tree import TaskContent, utc_now
from app.repository.question_tree import QuestionTreeConflict, QuestionTreeNotFound, _CONTENT, _date, _decode, _dump

_REFERENCES = TypeAdapter(tuple[AttachmentReference, ...])


class QuestionTreeDeletionRepository:
    def __init__(self, trees):
        self.trees = trees

    async def delete(self, tree_id, owner_userid) -> tuple[AttachmentReference, ...]:
        try:
            return await self._delete(tree_id, owner_userid)
        finally:
            # 包含提交回执不确定和直接仓库调用；此前读事务也不能迟到发布旧树。
            self.trees.structure_cache.discard(owner_userid, tree_id)

    async def _delete(self, tree_id, owner_userid) -> tuple[AttachmentReference, ...]:
        async with self.trees._transaction() as cursor:
            # 同一接口可在进程重启后继续清理；清单的归属不能由客户端指定或推断。
            await cursor.execute('SELECT attachments FROM question_tree_deletions '
                'WHERE tree_id=%s AND owner_userid=%s FOR UPDATE', (tree_id, owner_userid))
            pending = await cursor.fetchone()
            if pending is not None:
                return _REFERENCES.validate_python(_decode(pending['attachments']))
            tree = await self.trees._owned_tree(cursor, tree_id, owner_userid, lock=True)
            if tree.kind != 'exploration':
                raise QuestionTreeNotFound('问答树不存在')
            await cursor.execute('SELECT c.content FROM question_node_contents AS c '
                'JOIN question_nodes AS n ON n.id=c.node_id WHERE n.tree_id=%s', (tree_id,))
            references = {}
            for row in await cursor.fetchall():
                content = _CONTENT.validate_python(_decode(row['content']))
                if isinstance(content, TaskContent):
                    for reference in content.attachments:
                        previous = references.setdefault(reference.file_id, reference)
                        if previous != reference:
                            raise QuestionTreeConflict('附件登记类型冲突，暂不能删除')
            for reference in references.values():
                # 正常入口禁止跨树借用 UUID；历史损坏数据也不能导致误删其他树附件。
                await cursor.execute('SELECT n.id FROM question_node_contents AS c '
                    'JOIN question_nodes AS n ON n.id=c.node_id WHERE n.tree_id<>%s '
                    "AND JSON_CONTAINS(c.content,%s,'$.attachments')=1 LIMIT 1",
                    (tree_id, _dump([{'file_id': reference.file_id}])))
                if await cursor.fetchone() is not None:
                    raise QuestionTreeConflict('附件存在跨树引用，暂不能删除')
            attachments = tuple(references.values())
            await cursor.execute('INSERT INTO question_tree_deletions '
                '(tree_id,owner_userid,attachments,created_at) VALUES(%s,%s,%s,%s)',
                (tree_id, owner_userid, _dump([ref.model_dump() for ref in attachments]), _date(utc_now())))
            # 检索缓存是可选部署表；存在则必须删除，其他数据库错误不能当作“未部署”。
            await cursor.execute("SELECT TABLE_NAME FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='question_memory_index'")
            if await cursor.fetchone() is not None:
                await cursor.execute('DELETE FROM question_memory_index WHERE tree_id=%s', (tree_id,))
            for table in ('question_node_memories', 'question_node_messages',
                          'question_node_generations', 'question_node_contents'):
                await cursor.execute(f'DELETE c FROM {table} AS c '
                    'JOIN question_nodes AS n ON n.id=c.node_id WHERE n.tree_id=%s', (tree_id,))
            # 仅在删除事务内拆除父子及接替节点的自引用；回滚会完整恢复原状态。
            # summary 的 CHECK 要求 parent 非空，因此同时转换为 task 后再批量删除。
            await cursor.execute("UPDATE question_nodes SET kind='task',parent_id=NULL,"
                "status='failed',superseded_by_node_id=NULL WHERE tree_id=%s", (tree_id,))
            await cursor.execute('DELETE FROM question_nodes WHERE tree_id=%s', (tree_id,))
            await cursor.execute('DELETE FROM question_workspaces WHERE tree_id=%s', (tree_id,))
            await cursor.execute('DELETE FROM question_trees WHERE id=%s AND owner_userid=%s', (tree_id, owner_userid))
            return attachments

    async def finish(self, tree_id, owner_userid):
        async with self.trees._transaction() as cursor:
            await cursor.execute('DELETE FROM question_tree_deletions WHERE tree_id=%s AND owner_userid=%s',
                                 (tree_id, owner_userid))
