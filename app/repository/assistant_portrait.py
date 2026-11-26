"""私有画像检索、数字 key 预留和幂等写入。"""
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

    async def add_portrait(
        self,
        *,
        owner_userid: str,
        subject_userid: str,
        preference: str,
        task_id: str,
        operation_id: str,
    ) -> PortraitRecord:
        """按 (task_id, operation_id) 幂等新增一条私有画像记录。"""
        owner = PortraitSubject(userid=owner_userid)
        subject = PortraitSubject(userid=subject_userid)
        op_hash = hashlib.sha256(operation_id.encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(['add_portrait', subject.userid, preference], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        async with self._database._transaction() as cursor:
            await cursor.execute(
                'SELECT owner_userid,fingerprint FROM assistant_portrait_actions '
                'WHERE task_id=%s AND operation_hash=%s FOR UPDATE',
                (task_id, op_hash),
            )
            old = await cursor.fetchone()
            if old:
                if old['owner_userid'] != owner.userid or old['fingerprint'] != fingerprint:
                    raise ValueError('已提交画像动作不能改写')
                await cursor.execute(
                    'SELECT `key`,preference FROM assistant_portraits '
                    'WHERE owner_userid=%s AND subject_userid=%s AND preference=%s ORDER BY `key` DESC LIMIT 1',
                    (owner.userid, subject.userid, preference),
                )
                row = await cursor.fetchone()
                if row is not None:
                    return PortraitRecord.model_validate(row)
            await cursor.execute('SELECT next_key FROM assistant_portrait_sequence WHERE id=1 FOR UPDATE')
            sequence = await cursor.fetchone()
            if sequence is None:
                raise ValueError('画像 key 序列尚未初始化')
            await cursor.execute('SELECT COALESCE(MAX(`key`),0) AS last_key FROM assistant_portraits')
            key = max(sequence['next_key'], (await cursor.fetchone())['last_key'] + 1)
            if key >= 2**64 - 1:
                raise OverflowError('画像数字 key 已耗尽')
            await cursor.execute('UPDATE assistant_portrait_sequence SET next_key=%s WHERE id=1', (key + 1,))
            await cursor.execute(
                'INSERT INTO assistant_portraits(`key`,owner_userid,subject_userid,preference) VALUES(%s,%s,%s,%s)',
                (key, owner.userid, subject.userid, preference),
            )
            await cursor.execute(
                'INSERT INTO assistant_portrait_actions(task_id,owner_userid,operation_hash,fingerprint) '
                'VALUES(%s,%s,%s,%s)',
                (task_id, owner.userid, op_hash, fingerprint),
            )
            return PortraitRecord(key=key, preference=preference)

    async def delete_portrait(
        self,
        *,
        owner_userid: str,
        key: int,
        task_id: str,
        operation_id: str,
    ) -> bool:
        """按 (task_id, operation_id) 幂等删除一条私有画像记录。"""
        owner = PortraitSubject(userid=owner_userid)
        op_hash = hashlib.sha256(operation_id.encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(['delete_portrait', key], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        async with self._database._transaction() as cursor:
            await cursor.execute(
                'SELECT owner_userid,fingerprint FROM assistant_portrait_actions '
                'WHERE task_id=%s AND operation_hash=%s FOR UPDATE',
                (task_id, op_hash),
            )
            old = await cursor.fetchone()
            if old:
                if old['owner_userid'] != owner.userid or old['fingerprint'] != fingerprint:
                    raise ValueError('已提交画像动作不能改写')
                return True
            await cursor.execute(
                'DELETE FROM assistant_portraits WHERE owner_userid=%s AND `key`=%s',
                (owner.userid, key),
            )
            deleted = cursor.rowcount > 0
            await cursor.execute(
                'INSERT INTO assistant_portrait_actions(task_id,owner_userid,operation_hash,fingerprint) '
                'VALUES(%s,%s,%s,%s)',
                (task_id, owner.userid, op_hash, fingerprint),
            )
            return deleted

    async def add_idempotent(
        self,
        *,
        owner_userid: str,
        subject_userid: str,
        preference: str,
        operation_id: str,
        task_id: str = 'tool_call',
    ) -> int:
        record = await self.add_portrait(
            owner_userid=owner_userid,
            subject_userid=subject_userid,
            preference=preference,
            task_id=task_id,
            operation_id=operation_id,
        )
        return record.key

    async def delete_idempotent(
        self,
        *,
        owner_userid: str,
        key: int,
        operation_id: str,
        task_id: str = 'tool_call',
    ) -> bool:
        return await self.delete_portrait(
            owner_userid=owner_userid,
            key=key,
            task_id=task_id,
            operation_id=operation_id,
        )

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
