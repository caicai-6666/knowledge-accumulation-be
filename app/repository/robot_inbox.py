"""钉钉接收去重与待发布账本，文件位于 UploadFileStore；网络调用不占用 SQL 事务。"""
from datetime import datetime, timezone
from app.model.robot_inbox import RobotInboxRecord
from app.repository.question_tree import _date, _dump, _decode
from app.repository.assistant_runtime import MySQLAssistantRepository


def _record(row):
    values=dict(row)
    values['files']=_decode(values['files'])
    for key,value in values.items():
        if isinstance(value,datetime):values[key]=value.replace(tzinfo=timezone.utc)
    return RobotInboxRecord.model_validate(values)


class RobotInboxRepository:
    def __init__(self,trees):
        self.trees=trees
        self.locks=MySQLAssistantRepository(trees)

    def scope(self,request_id):
        # 独立消息锁，用户正在生成时仍可接收其后续短消息。
        return self.locks.user_scope(request_id,namespace='robot-inbox')

    async def validate_schema(self):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT request_id,userid,fingerprint,message,files,status,reply_text,'
                'publish_started_at,next_attempt_at,created_at,updated_at FROM assistant_robot_inbox LIMIT 0')

    async def get(self,request_id):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_robot_inbox WHERE request_id=%s',(request_id,))
            row=await cursor.fetchone()
            return _record(row) if row else None

    async def create(self,record):
        async with self.trees._transaction() as cursor:
            await cursor.execute('INSERT INTO assistant_robot_inbox '
                '(request_id,userid,fingerprint,message,files,status,reply_text,publish_started_at,next_attempt_at,created_at,updated_at) '
                'VALUES(%s,%s,%s,%s,%s,%s,NULL,NULL,%s,%s,%s)',
                (record.request_id,record.userid,record.fingerprint,record.message,
                 _dump([f.model_dump() for f in record.files]),record.status,_date(record.next_attempt_at),
                 _date(record.created_at),_date(record.updated_at)))
        return record

    async def update(self,request_id,**changes):
        allowed={'files','status','reply_text','publish_started_at','next_attempt_at','updated_at'}
        if not changes or not set(changes)<=allowed:raise ValueError('入站账本更新字段不合法')
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_robot_inbox WHERE request_id=%s FOR UPDATE',(request_id,))
            row=await cursor.fetchone()
            if row is None:raise ValueError('入站记录不存在')
            record=RobotInboxRecord.model_validate({**_record(row).model_dump(),**changes})
            values=record.model_dump()
            for key in changes:
                if key=='files':values[key]=_dump(values[key])
                elif isinstance(values[key],datetime):values[key]=_date(values[key])
            await cursor.execute('UPDATE assistant_robot_inbox SET '+','.join(f'{key}=%s' for key in changes)+' WHERE request_id=%s',
                                 (*[values[key] for key in changes],request_id))
            return record

    async def pending(self,*,now,retry_cutoff,limit):
        async with self.trees._transaction() as cursor:
            await cursor.execute("SELECT request_id FROM assistant_robot_inbox WHERE next_attempt_at<=%s AND "
                "(status IN ('publish_pending','reply_pending') OR "
                "(status='publish_unknown' AND publish_started_at>=%s) OR "
                "(status IN ('published','rejected') AND JSON_LENGTH(files)>0)) "
                'ORDER BY next_attempt_at,request_id LIMIT %s',(_date(now),_date(retry_cutoff),limit))
            return [r['request_id'] for r in await cursor.fetchall()]
