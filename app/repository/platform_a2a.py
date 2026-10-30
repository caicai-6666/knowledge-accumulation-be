"""A2A 私有账本；回复持久化先于中间件确认，模型接受状态独立记录。"""
import json
from datetime import timezone


def decode(row):
    if row is None:
        return None
    row = dict(row)
    for key in ('request_json', 'targets', 'published', 'replies', 'result_json'):
        if isinstance(row[key], str):
            row[key] = json.loads(row[key])
    for key in ('created_at', 'deadline_at', 'finished_at'):
        if row[key] is not None:
            row[key] = row[key].replace(tzinfo=timezone.utc)
    return row


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class PlatformA2ARepository:
    def __init__(self, database):
        self.database = database

    async def validate_schema(self):
        async with self.database._transaction() as c:
            await c.execute('SELECT broadcast_id,userid,tree_id,node_id,request_json,targets,published,replies,'
                'result_json,state,accepted,close_pending,created_at,deadline_at,finished_at FROM platform_a2a_calls LIMIT 0')

    async def get(self, identity):
        async with self.database._transaction() as c:
            await c.execute('SELECT * FROM platform_a2a_calls WHERE broadcast_id=%s', (identity,))
            return decode(await c.fetchone())

    async def create(self, *, request, tree_id, node_id, targets, now, deadline):
        body = request.model_dump(mode='json', exclude_none=True)
        async with self.database._transaction() as c:
            await c.execute('INSERT INTO platform_a2a_calls(broadcast_id,userid,tree_id,node_id,request_json,targets,'
                'replies,created_at,deadline_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE broadcast_id=platform_a2a_calls.broadcast_id',
                (request.broadcast_id, request.userid, tree_id, node_id, dump(body), dump(targets), '{}',
                 now.replace(tzinfo=None), deadline.replace(tzinfo=None)))
            await c.execute('SELECT * FROM platform_a2a_calls WHERE broadcast_id=%s FOR UPDATE', (request.broadcast_id,))
            saved = decode(await c.fetchone())
            if (saved['userid'], saved['tree_id'], saved['node_id'], saved['request_json'], saved['targets']) != (
                    request.userid, tree_id, node_id, body, targets):
                raise ValueError('同一工具调用不能改变请求内容')
            return saved

    async def update(self, identity, **changes):
        allowed = {'state', 'published', 'result_json', 'finished_at', 'close_pending'}
        if not changes or set(changes)-allowed:
            raise ValueError('不合法的 A2A 状态变更')
        values = []
        for key, value in changes.items():
            values.append(dump(value) if key in ('published', 'result_json') else
                          value.replace(tzinfo=None) if key == 'finished_at' and value is not None else value)
        async with self.database._transaction() as c:
            await c.execute('UPDATE platform_a2a_calls SET '+','.join(f'{k}=%s' for k in changes)+
                            ' WHERE broadcast_id=%s', (*values, identity))

    async def pending(self):
        async with self.database._transaction() as c:
            await c.execute("SELECT * FROM platform_a2a_calls WHERE state IN ('pending','waiting') "
                            'OR close_pending=TRUE ORDER BY created_at,broadcast_id LIMIT 100')
            return [decode(row) for row in await c.fetchall()]

    async def add_reply(self, identity, reply):
        payload = reply.model_dump(mode='json')
        async with self.database._transaction() as c:
            await c.execute('SELECT * FROM platform_a2a_calls WHERE broadcast_id=%s FOR UPDATE', (identity,))
            call = decode(await c.fetchone())
            if call is None or call['published'] is None:
                return False
            tasks = {t['target_platform_code']: t['task_id'] for t in call['published']['tasks']}
            if (reply.broadcast_id != identity or reply.userid != call['userid']
                    or tasks.get(reply.platform_code) != reply.task_id):
                raise ValueError('跨平台反馈与原请求不匹配')
            old = call['replies'].get(reply.platform_code)
            if old is not None and old != payload:
                raise ValueError('平台终态反馈不能改写')
            call['replies'][reply.platform_code] = payload
            # 迟到反馈仅补私有账本，不改已冻结的工具结果；可安全确认并释放共享窗口。
            await c.execute('UPDATE platform_a2a_calls SET replies=%s WHERE broadcast_id=%s',
                            (dump(call['replies']), identity))
            return True

    async def accept(self, identity):
        async with self.database._transaction() as c:
            await c.execute("UPDATE platform_a2a_calls SET accepted=TRUE WHERE broadcast_id=%s AND state='completed'", (identity,))

    async def has_context(self, userid, tree_id, platform, response_id):
        async with self.database._transaction() as c:
            # 只允许本用户、本树已接受结果中实际返回的成功上下文；不信任模型自报 ID。
            await c.execute("""SELECT 1 FROM platform_a2a_calls calls
                JOIN JSON_TABLE(calls.result_json,'$.replies[*]' COLUMNS(
                    platform_code VARCHAR(128) PATH '$.platform_code',
                    status VARCHAR(32) PATH '$.status', response_id VARCHAR(128) PATH '$.response_id')) r
                WHERE calls.userid=%s AND calls.tree_id=%s AND calls.accepted=TRUE
                    AND CAST(r.platform_code AS BINARY)=CAST(%s AS BINARY) AND r.status='succeeded'
                    AND CAST(r.response_id AS BINARY)=CAST(%s AS BINARY)
                    AND NOT EXISTS (SELECT 1 FROM platform_a2a_calls expired
                        WHERE expired.userid=calls.userid AND expired.tree_id=calls.tree_id
                        AND CAST(JSON_UNQUOTE(JSON_EXTRACT(expired.request_json,'$.response_id')) AS BINARY)=CAST(%s AS BINARY)
                        AND CAST(JSON_UNQUOTE(JSON_EXTRACT(expired.request_json,'$.response_platform_code')) AS BINARY)=CAST(%s AS BINARY)
                        AND JSON_CONTAINS(expired.result_json,JSON_OBJECT('platform_code',%s,'status','context_expired'),'$.replies'))
                LIMIT 1""", (userid, tree_id, platform, response_id, response_id, platform, platform))
            return await c.fetchone() is not None
