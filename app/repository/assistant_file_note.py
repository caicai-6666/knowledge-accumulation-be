"""个人文件备忘独立事务增删与字段全文检索；所有操作限定当前记录者。"""
from datetime import timezone
from pydantic import TypeAdapter
from app.model.assistant_file_note import AssistantFileNote
from app.model.question_file import FileID
from app.model.uploaded_file import FileOwnerID
from app.model.question_tree import utc_now
from app.repository.question_tree import _date


class FileNoteRejected(ValueError):
    def __init__(self, reason, required_action):
        self.reason, self.required_action = reason, required_action
        super().__init__(reason + required_action)


class FileNoteWriteUncertain(RuntimeError):
    """可能已提交；不得自动重试或将结果解释为未修改。"""


class AssistantFileNoteRepository:
    def __init__(self, database, *, clock=utc_now):
        self.database, self.clock = database, clock

    async def add(self, owner_userid, file_id, name, summary):
        note = AssistantFileNote(owner_userid=owner_userid, file_id=file_id,
            name=name, summary=summary, updated_at=self.clock())
        attempted = False
        try:
            async with self.database._transaction() as cursor:
                # 锁定文件行使同一文件的新增串行，并与文件删除协调；唯一键仍为最终保障。
                await cursor.execute('SELECT owner_userid,uploaded_at FROM uploaded_files WHERE id=%s FOR UPDATE', (note.file_id,))
                uploaded = await cursor.fetchone()
                if uploaded is None:
                    raise FileNoteRejected('未找到该文件的上传登记记录，本次未新增。',
                        '请核对完整文件 ID；确认无误后需先完成个人助理附件登记，不能据此判断原文件不存在。')
                if uploaded['owner_userid'] != note.owner_userid:
                    raise FileNoteRejected('当前只能为所属用户上传的文件创建备忘，本次未新增。',
                        '请改用所属用户上传的文件 ID；知道他人的公开文件 ID 不代表可以记账。')
                await cursor.execute('SELECT file_id FROM assistant_file_notes WHERE owner_userid=%s AND file_id=%s FOR UPDATE',
                    (note.owner_userid, note.file_id))
                if await cursor.fetchone() is not None:
                    raise FileNoteRejected('当前用户已为该文件创建备忘，本次未新增、未覆盖。',
                        '请核对文件 ID；如需替换，先确认原记录和替换意图，不要自动删除重建。')
                attempted = True
                await cursor.execute('INSERT INTO assistant_file_notes(owner_userid,file_id,name,summary,updated_at) VALUES(%s,%s,%s,%s,%s)',
                    (note.owner_userid, note.file_id, note.name, note.summary, _date(note.updated_at)))
            return note, uploaded['uploaded_at'].replace(tzinfo=timezone.utc)
        except Exception as error:
            if not attempted:
                raise
            raise FileNoteWriteUncertain('备忘新增结果尚未确认，不能确认是否已创建。') from error

    async def delete(self, owner_userid, file_id):
        owner_userid = TypeAdapter(FileOwnerID).validate_python(owner_userid)
        file_id = TypeAdapter(FileID).validate_python(file_id)
        attempted = False
        try:
            async with self.database._transaction() as cursor:
                # 删除只按记录者限定；不要求文件公开，也不读取他人的记录。
                await cursor.execute('SELECT file_id FROM assistant_file_notes WHERE owner_userid=%s AND file_id=%s FOR UPDATE',
                    (owner_userid, file_id))
                if await cursor.fetchone() is None:
                    return False
                attempted = True
                await cursor.execute('DELETE FROM assistant_file_notes WHERE owner_userid=%s AND file_id=%s', (owner_userid, file_id))
            return True
        except Exception as error:
            if not attempted:
                raise
            raise FileNoteWriteUncertain('备忘删除结果尚未确认，不能确认是否已删除。') from error

    async def search(self, owner_userid, *, file_ids=None, name_keyword=None, summary_keyword=None,
                     match_mode='and', time_field='updated_at', start_time=None, end_time=None, limit=1001):
        from app.model.assistant_file_note import FileNoteSearchRecord
        owner_userid = TypeAdapter(FileOwnerID).validate_python(owner_userid)
        if match_mode not in ('and', 'or') or time_field not in ('uploaded_at', 'updated_at'):
            raise ValueError('备忘查询组合或时间字段不合法')
        if type(limit) is not int or not 1 <= limit <= 1001:
            raise ValueError('备忘查询数量不合法')
        clauses, values = ['n.owner_userid=%s'], [owner_userid]
        if file_ids is not None:
            file_ids = TypeAdapter(list[FileID]).validate_python(list(file_ids))
            if not file_ids:
                return []
            if len(file_ids) > 1000:
                raise ValueError('备忘筛选范围超过上限')
            clauses.append('n.file_id IN (' + ','.join('%s' for _ in file_ids) + ')')
            values.extend(file_ids)
        terms = []
        # 两路单列 MATCH 分别绑定对应索引，不能用联合 MATCH 代替 AND/OR。
        for column, keyword in (('name', name_keyword), ('summary', summary_keyword)):
            if keyword:
                terms.append(f'MATCH(n.{column}) AGAINST(%s IN NATURAL LANGUAGE MODE)>0')
                values.append(keyword)
        if terms:
            clauses.append('(' + (' AND ' if match_mode == 'and' else ' OR ').join(terms) + ')')
        column = 'u.uploaded_at' if time_field == 'uploaded_at' else 'n.updated_at'
        for boundary, operator in ((start_time, '>='), (end_time, '<')):
            if boundary is not None:
                if boundary.tzinfo is None or boundary.utcoffset() is None:
                    raise ValueError('备忘查询时间必须带时区')
                clauses.append(f'{column}{operator}%s')
                values.append(_date(boundary))
        if start_time is not None and end_time is not None and start_time >= end_time:
            raise ValueError('备忘查询结束时间必须晚于开始时间')
        async with self.database._transaction() as cursor:
            await cursor.execute('SELECT n.owner_userid,n.file_id,n.name,n.summary,n.updated_at,u.uploaded_at '
                'FROM assistant_file_notes AS n JOIN uploaded_files AS u ON u.id=n.file_id WHERE '
                + ' AND '.join(clauses) + ' ORDER BY n.updated_at DESC,n.file_id ASC LIMIT %s', (*values, limit))
            rows = await cursor.fetchall()
        return [FileNoteSearchRecord.model_validate({**row,
            'uploaded_at':row['uploaded_at'].replace(tzinfo=timezone.utc),
            'updated_at':row['updated_at'].replace(tzinfo=timezone.utc)}) for row in rows]
