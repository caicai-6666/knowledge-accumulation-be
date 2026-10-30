"""私有画像检索、数字 key 预留和检查点同事务写入。"""
import hashlib
import json

from app.model.assistant_portrait import PortraitGroup, PortraitRecord, PortraitSubject


class AssistantPortraitRepository:
    def __init__(self, database):
        self._database = database

    async def validate_schema(self):
        async with self._database._transaction() as cursor:
            await cursor.execute('SELECT `key`,owner_userid,subject_userid,preference FROM assistant_portraits LIMIT 0')
            await cursor.execute('SELECT id,next_key FROM assistant_portrait_sequence WHERE id=1')
            if await cursor.fetchone() is None:
                raise ValueError('画像 key 序列尚未初始化')
            await cursor.execute('SELECT task_id,owner_userid,operation_hash,fingerprint FROM assistant_portrait_actions LIMIT 0')

    async def target_exists(self, userid):
        target = PortraitSubject(userid=userid)
        async with self._database._transaction() as cursor:
            await cursor.execute('SELECT userid FROM employees WHERE userid=%s', (target.userid,))
            return await cursor.fetchone() is not None

    async def find(self, owner_userid, key):
        owner = PortraitSubject(userid=owner_userid)
        from app.agent.assistant.tool.portrait_contract import DeletePortraitArguments
        key = DeletePortraitArguments(key=key).key
        async with self._database._transaction() as cursor:
            await cursor.execute('SELECT `key`,preference FROM assistant_portraits WHERE owner_userid=%s AND `key`=%s',
                                 (owner.userid, key))
            row = await cursor.fetchone()
            return PortraitRecord.model_validate(row) if row else None

    async def reserve_key(self):
        """独立预留稳定数字，仅推进序列；动作失败或取消可留空号，不写画像正文。"""
        async with self._database._transaction() as cursor:
            await cursor.execute('SELECT next_key FROM assistant_portrait_sequence WHERE id=1 FOR UPDATE')
            sequence = await cursor.fetchone()
            if sequence is None:
                raise ValueError('画像 key 序列尚未初始化')
            await cursor.execute('SELECT COALESCE(MAX(`key`),0) AS last_key FROM assistant_portraits')
            key = max(sequence['next_key'], (await cursor.fetchone())['last_key'] + 1)
            if key >= 2**64 - 1:
                raise OverflowError('画像数字 key 已耗尽')
            await cursor.execute('UPDATE assistant_portrait_sequence SET next_key=%s WHERE id=1', (key + 1,))
            return key

    async def commit_actions(self, cursor, record, content):
        """仅处理权威已接受工具对；变更与动作账本由调用方的检查点事务一起提交。"""
        from app.agent.assistant.tool.portrait_contract import (
            AddPortraitArguments, AddPortraitResult, DeletePortraitArguments, DeletePortraitResult)
        receipts = {item['tool_call_id']: item['content'] for item in content.exchanges if item.get('role') == 'tool'}
        for message in content.exchanges:
            if message.get('role') != 'assistant':
                continue
            for call in message.get('tool_calls', ()):
                name = call['function']['name']
                if name not in ('add_portrait', 'delete_portrait'):
                    continue
                arguments = (AddPortraitArguments if name == 'add_portrait' else DeletePortraitArguments
                             ).model_validate_json(call['function']['arguments'])
                payload = json.loads(receipts[call['id']])
                # Core 可追加工作区提示；只投影业务回执字段，不把其当作画像内容。
                fields = ('subject', 'portrait') if name == 'add_portrait' else ('key', 'deleted')
                receipt = (AddPortraitResult if name == 'add_portrait' else DeletePortraitResult
                           ).model_validate({key: payload[key] for key in fields})
                fingerprint = hashlib.sha256(json.dumps([name, arguments.model_dump(mode='json'),
                    receipt.model_dump(mode='json')], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                operation = hashlib.sha256(call['id'].encode()).hexdigest()
                await cursor.execute('SELECT owner_userid,fingerprint FROM assistant_portrait_actions '
                    'WHERE task_id=%s AND operation_hash=%s FOR UPDATE', (record.task_id, operation))
                old = await cursor.fetchone()
                if old:
                    if old['owner_userid'] != record.userid or old['fingerprint'] != fingerprint:
                        raise ValueError('已提交画像动作不能改写')
                    continue
                if name == 'add_portrait':
                    if receipt.subject != arguments.subject or receipt.portrait.preference != arguments.preference:
                        raise ValueError('新增画像回执与请求不符')
                    await cursor.execute('INSERT INTO assistant_portraits(`key`,owner_userid,subject_userid,preference) '
                        'VALUES(%s,%s,%s,%s)', (receipt.portrait.key, record.userid,
                         arguments.subject.userid, arguments.preference))
                elif receipt.key != arguments.key:
                    raise ValueError('删除画像回执与请求不符')
                elif receipt.deleted:
                    await cursor.execute('DELETE FROM assistant_portraits WHERE owner_userid=%s AND `key`=%s',
                                         (record.userid, arguments.key))
                await cursor.execute('INSERT INTO assistant_portrait_actions(task_id,owner_userid,operation_hash,fingerprint) '
                    'VALUES(%s,%s,%s,%s)', (record.task_id, record.userid, operation, fingerprint))

    async def search(self, owner_userid: str, name: str, *, limit: int = 1001) -> list[PortraitGroup]:
        owner = PortraitSubject(userid=owner_userid)
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError('画像名称查询条件不合法')
        if type(limit) is not int or limit < 1:
            raise ValueError('画像查询数量不合法')
        keyword = name.strip()
        # 助理只是员工的代理，名称后缀不参与人员全文检索，不产生另一份目标身份。
        if keyword.endswith('的助理'):
            keyword = keyword[:-3].strip()
        if not keyword:
            raise ValueError('请提供具体员工或助理名称')
        literal = keyword.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        async with self._database._transaction() as cursor:
            # EXISTS 先按私有归属约束对象，不能先截取全员目录再筛画像，否则会漏召回。
            await cursor.execute(
                'SELECT e.userid,e.name FROM employees AS e WHERE EXISTS ('
                'SELECT 1 FROM assistant_portraits AS p WHERE p.owner_userid=%s AND p.subject_userid=e.userid) '
                "AND (MATCH(e.name) AGAINST(%s IN NATURAL LANGUAGE MODE)>0 OR LOWER(e.name) LIKE LOWER(%s) ESCAPE '!') "
                'ORDER BY (LOWER(e.name)=LOWER(%s)) DESC, '
                'MATCH(e.name) AGAINST(%s IN NATURAL LANGUAGE MODE) DESC,e.name,e.userid LIMIT %s',
                (owner.userid, keyword, '%' + literal + '%', keyword, keyword, limit))
            subjects = await cursor.fetchall()
            if not subjects:
                return []
            ids = tuple(row['userid'] for row in subjects)
            placeholders = ','.join('%s' for _ in ids)
            await cursor.execute(
                'SELECT `key`,subject_userid,preference FROM assistant_portraits '
                'WHERE owner_userid=%s AND subject_userid IN (' + placeholders + ') ORDER BY `key`',
                (owner.userid, *ids))
            records = {userid: [] for userid in ids}
            for row in await cursor.fetchall():
                records[row['subject_userid']].append(PortraitRecord(key=row['key'], preference=row['preference']))
            return [PortraitGroup(subject=PortraitSubject(userid=row['userid']), name=row['name'],
                                  portraits=tuple(records[row['userid']])) for row in subjects]
