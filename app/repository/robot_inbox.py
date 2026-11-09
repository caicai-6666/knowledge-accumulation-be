"""钉钉接收去重与待发布账本，文件位于 UploadFileStore；网络调用不占用 SQL 事务。"""
from datetime import datetime, timezone
from app.model.robot_inbox import RobotInboxRecord
from app.model.question_tree import utc_now
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

    def user_scope(self, userid):
        # 登记与发布共用用户锁，防止较早 INSERT 尚未提交时后序抢先通过检查。
        return self.locks.user_scope(userid, namespace='robot-user')

    async def validate_schema(self):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT request_id,userid,fingerprint,message,files,status,reply_text,'
                'publish_started_at,next_attempt_at,created_at,updated_at,arrival_sequence FROM assistant_robot_inbox LIMIT 0')
            await cursor.execute("SHOW COLUMNS FROM assistant_robot_inbox LIKE 'status'")
            column = await cursor.fetchone()
            if column is None or "'cancelled'" not in column['Type']:
                raise ValueError('机器人接收表缺少 cancelled 终止状态；新空库请使用 deploy/mysql/init/001-schema.sql 初始化，已有数据库请先升级该状态枚举和取消约束，再启动接收服务')

    async def cancel_received(self, request_id, *, userid, reason):
        """运维明确终止未发布的接收；与接收/发布共用锁，不删除资料或伪造发送。"""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('必须填写不超过1000字符的具体终止原因')
        async with self.scope(request_id) as held:
            if not held:
                raise ValueError('该消息正在接收或处理，未终止；请等待本次操作结束后核对')
            async with self.user_scope(userid) as held:
                if not held:
                    raise ValueError('该用户正在登记或发布消息，未终止；请稍后重新核对')
                async with self.trees._transaction() as cursor:
                    await cursor.execute('SELECT * FROM assistant_robot_inbox WHERE request_id=%s FOR UPDATE',
                                         (request_id,))
                    row = await cursor.fetchone()
                    if row is None or row['userid'] != userid:
                        raise ValueError('未找到该用户的入站消息，未终止')
                    record = _record(row)
                    if record.status == 'cancelled':
                        return record
                    if record.status != 'received' or record.publish_started_at is not None:
                        raise ValueError('仅允许终止尚未尝试发布的 received 消息；请先核对交付结果，不能跳过未知发送')
                    now = utc_now()
                    await cursor.execute("UPDATE assistant_robot_inbox SET status='cancelled',reply_text=%s,updated_at=%s "
                                         'WHERE request_id=%s', (reason, _date(now), request_id))
                    return RobotInboxRecord.model_validate({**record.model_dump(),
                        'status':'cancelled', 'reply_text':reason, 'updated_at':now})

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
            await cursor.execute('SELECT * FROM assistant_robot_inbox WHERE request_id=%s', (record.request_id,))
            stored = _record(await cursor.fetchone())
        return stored

    async def has_unpublished_predecessor(self, record):
        if record.arrival_sequence is None:
            raise ValueError('入站消息缺少持久化顺序，不能发布')
        async with self.trees._transaction() as cursor:
            await cursor.execute(
                "SELECT 1 FROM assistant_robot_inbox WHERE userid=%s AND arrival_sequence<%s "
                "AND status IN ('received','publish_pending','publish_unknown','publish_blocked') LIMIT 1",
                (record.userid, record.arrival_sequence))
            return await cursor.fetchone() is not None

    async def update(self,request_id,**changes):
        allowed={'message','files','status','reply_text','publish_started_at','next_attempt_at','updated_at'}
        if not changes or not set(changes)<=allowed:raise ValueError('入站账本更新字段不合法')
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_robot_inbox WHERE request_id=%s FOR UPDATE',(request_id,))
            row=await cursor.fetchone()
            if row is None:raise ValueError('入站记录不存在')
            current = _record(row)
            if 'message' in changes and (current.status != 'received' or current.publish_started_at is not None):
                raise ValueError('仅接收准备阶段可补充已验证的引用文件，发布后正文不可变')
            if current.status == 'cancelled':
                raise ValueError('已终止接收的消息不能恢复、重放或覆盖终止记录')
            if changes.get('status') == 'cancelled':
                raise ValueError('终止接收必须使用带消息锁和用户锁的 cancel_received')
            record=RobotInboxRecord.model_validate({**current.model_dump(),**changes})
            values=record.model_dump()
            for key in changes:
                if key=='files':values[key]=_dump(values[key])
                elif isinstance(values[key],datetime):values[key]=_date(values[key])
            await cursor.execute('UPDATE assistant_robot_inbox SET '+','.join(f'{key}=%s' for key in changes)+' WHERE request_id=%s',
                                 (*[values[key] for key in changes],request_id))
            return record

    async def pending(self,*,now,retry_cutoff,limit):
        async with self.trees._transaction() as cursor:
            # 先筛掉被本用户前序阻塞的记录，再 LIMIT；避免一个用户占满扫描批次。
            await cursor.execute(
                "SELECT candidate.request_id FROM assistant_robot_inbox AS candidate "
                "WHERE candidate.next_attempt_at<=%s AND ("
                "(candidate.status IN ('publish_pending','publish_unknown') "
                "AND (candidate.status<>'publish_unknown' OR candidate.publish_started_at>=%s) "
                "AND NOT EXISTS (SELECT 1 FROM assistant_robot_inbox AS earlier "
                "WHERE earlier.userid=candidate.userid AND earlier.arrival_sequence<candidate.arrival_sequence "
                "AND earlier.status IN ('received','publish_pending','publish_unknown','publish_blocked'))) "
                "OR candidate.status='reply_pending' "
                "OR (candidate.status IN ('published','rejected') AND JSON_LENGTH(candidate.files)>0)) "
                "ORDER BY candidate.arrival_sequence LIMIT %s",
                (_date(now), _date(retry_cutoff), limit))
            return [r['request_id'] for r in await cursor.fetchall()]
