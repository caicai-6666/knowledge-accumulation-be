"""复用项目数据库事务，幂等登记上传文件，不修改已有权限或上传时间。"""
from datetime import timedelta, timezone

from asyncmy.errors import IntegrityError

from app.model.uploaded_file import UploadedFileRecord
from app.repository.question_tree import _date


class UploadedFileConflict(ValueError):
    """文件 UUID 已存在，但归属或类型不符。"""


class UploadedFileAccessError(ValueError):
    """传递校验分支确定的错误码和修正方向，不在下游合并业务原因。"""
    def __init__(self, code, reason, required_action):
        self.code = code
        self.reason = reason
        self.required_action = required_action
        super().__init__(reason + required_action)


class FileVisibilityUpdateUncertain(RuntimeError):
    """已尝试更新但未确认提交，调用方不得报告成功或自动重放。"""


def _record(row):
    return UploadedFileRecord.model_validate({**row,
        'uploaded_at': row['uploaded_at'].replace(tzinfo=timezone.utc),
        'updated_at': row['updated_at'].replace(tzinfo=timezone.utc)})


class UploadedFileRepository:
    def __init__(self, database):
        self.database = database

    async def register(self, record: UploadedFileRecord) -> UploadedFileRecord:
        async with self.database._transaction() as cursor:
            # 唯一键负责串行化并发重试；重复登记不重置 private、首次上传时间或更新时间。
            await cursor.execute(
                'INSERT INTO uploaded_files(id,owner_userid,type,visibility,uploaded_at,updated_at) '
                'VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id',
                (record.id, record.owner_userid, record.type, record.visibility,
                 _date(record.uploaded_at), _date(record.updated_at)))
            await cursor.execute('SELECT * FROM uploaded_files WHERE id=%s FOR UPDATE', (record.id,))
            row = await cursor.fetchone()
            if (row['owner_userid'], row['type']) != (record.owner_userid, record.type):
                raise UploadedFileConflict('文件已登记为其他用户或类型，不能覆盖；请核对可信上传来源和文件映射')
            return _record(row)

    async def _owned(self, cursor, file_id, owner_userid, *, lock=False):
        await cursor.execute('SELECT * FROM uploaded_files WHERE id=%s'
                             + (' FOR UPDATE' if lock else ''), (file_id,))
        row = await cursor.fetchone()
        if row is None:
            # 只能确认元信息表中无记录，不能据此判断原文件不存在或仍在上传。
            raise UploadedFileAccessError('file_not_registered',
                '未找到该文件的上传登记记录。' + ('本次未修改。' if lock else '本次未返回可见范围。'),
                '请核对完整文件 ID；ID 确认无误后，需先完成文件登记再操作，不要猜测当前可见范围。')
        if row['owner_userid'] != owner_userid:
            # 区分已确认的权限拒绝，但不返回上传者身份、可见范围或其他元信息。
            raise UploadedFileAccessError('file_permission_denied',
                '该文件不属于当前所属用户，无权' + ('修改可见范围，本次未修改。' if lock else '查看可见范围，本次未返回文件信息。'),
                '请由文件上传者的助理操作；如选错文件，请改用所属用户上传的文件 ID。')
        return _record(row)

    async def get_owned(self, file_id, owner_userid):
        async with self.database._transaction() as cursor:
            return await self._owned(cursor, file_id, owner_userid)

    async def set_visibility(self, file_id, owner_userid, visibility, *, now):
        attempted = False
        try:
            async with self.database._transaction() as cursor:
                current = await self._owned(cursor, file_id, owner_userid, lock=True)
                if current.visibility == visibility:
                    return current, False
                # 同一文件锁内核对归属并更新；无变化时不刷新时间，变更不重置上传时间。
                updated_at = max(now, current.updated_at + timedelta(microseconds=1))
                attempted = True
                await cursor.execute('UPDATE uploaded_files SET visibility=%s,updated_at=%s '
                    'WHERE id=%s AND owner_userid=%s', (visibility, _date(updated_at), file_id, owner_userid))
                updated = current.model_copy(update={'visibility': visibility, 'updated_at': updated_at})
            return updated, True
        except Exception as error:
            if not attempted:
                raise
            # 包含提交回执丢失；即使连接尝试回滚，也不能据此断言变更未生效。
            raise FileVisibilityUpdateUncertain(
                '文件可见范围修改结果未确认，不能声称已修改或自动重试；恢复后先用 get_file_visibility 核对当前值，再决定是否需要修改。') from error

    async def remove(self, reference, owner_userid):
        """仅供宿主补偿/整树清理，先确认 SQL 删除成功，再允许清理磁盘。"""
        async with self.database._transaction() as cursor:
            await cursor.execute('SELECT owner_userid,type FROM uploaded_files WHERE id=%s FOR UPDATE',
                                 (reference.file_id,))
            row = await cursor.fetchone()
            if row is None:
                return  # 历史附件可能尚无元信息；重复补偿也应可继续清理磁盘。
            if (row['owner_userid'], row['type']) != (owner_userid, reference.type):
                raise UploadedFileConflict('文件归属或类型不符，未删除元信息；请核对文件清理清单')
            try:
                await cursor.execute('DELETE FROM uploaded_files WHERE id=%s', (reference.file_id,))
            except IntegrityError as error:
                if error.args[0] != 1451:
                    raise
                raise ValueError('文件仍有关联备忘，未删除元信息或原文件；请先处理关联备忘再重试清理') from error
