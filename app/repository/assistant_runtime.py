"""助理绑定、执行账本与交付记录；外部网络操作始终在数据库事务之外。"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid5, NAMESPACE_URL

import asyncmy
from asyncmy.cursors import DictCursor
from pydantic import SecretStr
from app.model.assistant import Assistant, AssistantTaskExecution, AssistantDelivery, TaskFileBinding
from app.model.assistant_queue import AssistantDeliveryLease
from app.model.question_tree import QuestionTree, utc_now
from app.repository.question_tree import _row, _date, _dump, _decode
from app.agent.assistant.tool.send_assistant_message import SendAssistantMessageArguments
from app.agent.assistant.tool.finish_task import FinishAssistantTaskArguments
from .assistant_reminder import MySQLReminderRepository
from .assistant_portrait import AssistantPortraitRepository


def task_node_id(task_id):
    return uuid5(NAMESPACE_URL,'knowledge-assistant-task:'+task_id).hex


@dataclass(frozen=True)
class StoredAssistantTask:
    record: AssistantTaskExecution
    lease: AssistantDeliveryLease | None


def _task(row):
    value={key:item.replace(tzinfo=timezone.utc) if isinstance(item,datetime) else item for key,item in row.items()}
    token=value.pop('delivery_token')
    expiry=value.pop('lease_expires_at')
    value['input_payload']=_decode(value['input_payload'])
    value['file_bindings']=_decode(value['file_bindings'])
    record=_row(AssistantTaskExecution,value)
    lease=None if token is None else AssistantDeliveryLease(task=record.input_payload,
        delivery_token=SecretStr(token),lease_expires_at=expiry.replace(tzinfo=record.received_at.tzinfo))
    return StoredAssistantTask(record,lease)


def _delivery(row):
    value={key:item.replace(tzinfo=timezone.utc) if isinstance(item,datetime) else item for key,item in row.items()}
    if value['external_receipt'] is not None:value['external_receipt']=_decode(value['external_receipt'])
    return _row(AssistantDelivery,value)


class MySQLAssistantRepository:
    def __init__(self,trees):
        self.trees=trees
        self.reminders=MySQLReminderRepository(trees)
        self.portraits=AssistantPortraitRepository(trees)

    async def validate_schema(self):
        await self.reminders.validate_schema()
        await self.portraits.validate_schema()
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT tree_id,id,content,summary,first_sequence FROM assistant_workspaces LIMIT 0')
            await cursor.execute('SELECT userid,tree_id,enabled,heartbeat_interval_seconds FROM assistants LIMIT 0')
            await cursor.execute('SELECT task_id,delivery_token,lease_expires_at FROM assistant_task_executions LIMIT 0')
            await cursor.execute('SELECT id,action_key,status,error_code FROM assistant_deliveries LIMIT 0')
            await cursor.execute("SHOW INDEX FROM assistant_task_executions WHERE Key_name='uq_assistant_active'")
            if not await cursor.fetchone():raise ValueError('请先执行 012 助理消费迁移')

    @asynccontextmanager
    async def user_scope(self,userid,*,namespace='assistant'):
        """独立连接持有 MySQL 命名锁，跨本服务实例串行同一用户；连接丢失立即取消消费。"""
        await self.trees._initialize()
        settings=self.trees._settings
        if namespace not in ('assistant','robot-inbox'):raise ValueError('锁命名空间不合法')
        name=namespace+':'+hashlib.sha256((settings.database+'\0'+userid).encode()).hexdigest()[:48]
        connection=await asyncmy.connect(host=settings.host,port=settings.port,user=settings.user,
            password=settings.password.get_secret_value(),db=settings.database,autocommit=True,
            connect_timeout=settings.connect_timeout_seconds)
        owner=asyncio.current_task();monitor=None;held=False
        try:
            async with asyncio.timeout(settings.operation_timeout_seconds):
                async with connection.cursor() as cursor:
                    await cursor.execute('SELECT GET_LOCK(%s,0)',(name,))
                    held=(await cursor.fetchone())[0]==1
            if not held:
                yield False
                return
            async def watch():
                try:
                    while True:
                        await asyncio.sleep(1)
                        async with asyncio.timeout(settings.operation_timeout_seconds):
                            await connection.ping(reconnect=False)
                except Exception:
                    owner.cancel()
            monitor=asyncio.create_task(watch())
            yield True
        finally:
            if monitor is not None:
                monitor.cancel();await asyncio.gather(monitor,return_exceptions=True)
            # 关闭专用连接会释放其命名锁，不把锁带回共享连接池。
            try:
                async with asyncio.timeout(settings.operation_timeout_seconds):await connection.ensure_closed()
            except TimeoutError:connection.close()

    async def ensure(self,userid):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistants WHERE userid=%s',(userid,))
            row=await cursor.fetchone()
            if row:
                row=dict(row);row['enabled']=bool(row['enabled'])
                if row['enabled']:
                    # 用户工作循环初始化即恢复有效窗口，空闲等待队列时也保持驻留。
                    from app.repository.assistant_memory import load_memory
                    tree=await self.trees._owned_tree(cursor,row['tree_id'],userid)
                    await load_memory(self.trees,cursor,tree)
                else:
                    self.trees.assistant_memory_cache.discard(userid,row['tree_id'])
                return _row(Assistant,row)
            await cursor.execute("SELECT * FROM question_trees WHERE owner_userid=%s AND kind='assistant'",(userid,))
            rows=await cursor.fetchall()
            if len(rows)>1:raise ValueError('同一用户存在重复助理树')
            if rows:
                tree=_row(QuestionTree,rows[0])
            else:
                tree=QuestionTree(owner_userid=userid,name='个人助理',kind='assistant')
                await cursor.execute('INSERT INTO question_trees(id,owner_userid,name,kind,main_leaf_id,revision,created_at,updated_at) '
                    'VALUES(%s,%s,%s,%s,NULL,0,%s,%s)',(tree.id,userid,tree.name,tree.kind,_date(tree.created_at),_date(tree.updated_at)))
                from app.repository.assistant_memory import create_memory
                self.trees._stage_assistant_memory(cursor,await create_memory(cursor,tree))
            assistant=Assistant(userid=userid,tree_id=tree.id)
            await cursor.execute('INSERT INTO assistants(userid,tree_id,tree_kind,enabled,heartbeat_interval_seconds,created_at,updated_at) '
                'VALUES(%s,%s,%s,1,-1,%s,%s)',(userid,tree.id,'assistant',_date(assistant.created_at),_date(assistant.updated_at)))
            return assistant

    async def heartbeat_candidates(self):
        """只扫描已有且开启心跳的助理，不为扫描创建树或复制用户目录。"""
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT userid FROM assistants WHERE enabled=1 '
                                 'AND heartbeat_interval_seconds>0 ORDER BY userid')
            return [row['userid'] for row in await cursor.fetchall()]

    async def heartbeat_candidate(self, userid):
        """取得用户执行锁后重新核对配置；待核对的未确认任务不再积压心跳。"""
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT a.* FROM assistants a WHERE a.userid=%s AND a.enabled=1 '
                'AND a.heartbeat_interval_seconds>0 AND NOT EXISTS '
                '(SELECT 1 FROM assistant_task_executions t WHERE t.userid=a.userid AND t.acknowledged_at IS NULL)',
                (userid,))
            row = await cursor.fetchone()
            if row is None:
                return None
            row = dict(row); row['enabled'] = bool(row['enabled'])
            return _row(Assistant, row)

    async def recipient_enabled(self,userid):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT enabled FROM assistants WHERE userid=%s',(userid,))
            row=await cursor.fetchone()
            # 尚未创建绑定的真实员工可接收，消费者随后会创建其唯一助理。
            return row is None or bool(row['enabled'])

    async def get_task(self,task_id):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_task_executions WHERE task_id=%s',(task_id,))
            row=await cursor.fetchone()
            return _task(row) if row else None

    async def receive(self,assistant,lease):
        if assistant.userid!=lease.task.userid:raise ValueError('领取任务不属于当前助理')
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_task_executions WHERE task_id=%s FOR UPDATE',(lease.task.task_id,))
            row=await cursor.fetchone()
            if row:
                stored=_task(row)
                if stored.record.input_payload!=lease.task or stored.record.tree_id!=assistant.tree_id:
                    raise ValueError('重复领取对应不同的冻结任务或助理树')
            else:
                now=utc_now()
                await cursor.execute('INSERT INTO assistant_task_executions '
                    '(task_id,userid,tree_id,task_type,input_payload,file_bindings,processing_status,received_at,updated_at) '
                    "VALUES(%s,%s,%s,%s,%s,'[]','received',%s,%s)",
                    (lease.task.task_id,assistant.userid,assistant.tree_id,lease.task.task_type,
                     _dump(lease.task.model_dump(mode='json')),_date(now),_date(now)))
            await cursor.execute('UPDATE assistant_task_executions SET delivery_token=%s,lease_expires_at=%s WHERE task_id=%s',
                (lease.delivery_token.get_secret_value(),_date(lease.lease_expires_at),lease.task.task_id))
        return await self.get_task(lease.task.task_id)

    async def _save(self,cursor,record):
        record=AssistantTaskExecution.model_validate(record.model_dump())
        await cursor.execute('UPDATE assistant_task_executions SET node_id=%s,file_bindings=%s,processing_status=%s,error_code=%s,'
            'updated_at=%s,finished_at=%s,acknowledged_at=%s WHERE task_id=%s',
            (record.node_id,_dump([v.model_dump(mode='json') for v in record.file_bindings]),record.processing_status,
             record.error_code,_date(record.updated_at),_date(record.finished_at) if record.finished_at else None,
             _date(record.acknowledged_at) if record.acknowledged_at else None,record.task_id))

    async def update(self,task_id,**changes):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_task_executions WHERE task_id=%s FOR UPDATE',(task_id,))
            record=_task(await cursor.fetchone()).record
            await self._save(cursor,record.model_copy(update={**changes,'updated_at':utc_now()}))
        return (await self.get_task(task_id)).record

    async def bind_file(self,task_id,binding):
        stored=await self.get_task(task_id)
        current=[v for v in stored.record.file_bindings if v.source==binding.source]
        if current:
            if current[0]!=binding:raise ValueError('附件映射不能覆盖')
            return stored.record
        return await self.update(task_id,file_bindings=(*stored.record.file_bindings,binding))

    async def _insert_delivery(self,cursor,record,*,key,recipient,message,destination):
        await cursor.execute('SELECT * FROM assistant_deliveries WHERE task_id=%s AND action_key=%s',(record.task_id,key))
        row=await cursor.fetchone()
        if row:
            existing=_delivery(row)
            if (existing.recipient_userid,existing.message,existing.destination)!=(recipient,message,destination):
                raise ValueError('已接受发送动作不能覆盖')
            return
        item=AssistantDelivery(userid=record.userid,task_id=record.task_id,action_key=key,destination=destination,
            recipient_userid=recipient,message=message,task_type='assistant_message' if destination=='assistant' else None)
        await cursor.execute('INSERT INTO assistant_deliveries '
            '(id,userid,task_id,action_key,destination,recipient_userid,task_type,message,status,attempt_count,created_at,updated_at) '
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending',0,%s,%s)",
            (item.id,item.userid,item.task_id,item.action_key,item.destination,item.recipient_userid,item.task_type,
             item.message,_date(item.created_at),_date(item.updated_at)))

    def commit_hook(self,task_id):
        async def commit(cursor,tree,node_id,content,status):
            await cursor.execute('SELECT * FROM assistant_task_executions WHERE task_id=%s FOR UPDATE',(task_id,))
            record=_task(await cursor.fetchone()).record
            if (tree.id,tree.owner_userid,node_id)!=(record.tree_id,record.userid,task_node_id(task_id)):
                raise ValueError('检查点与助理任务身份不符')
            for message in content.exchanges:
                if message.get('role')!='assistant':continue
                for call in message.get('tool_calls',[]):
                    if call['function']['name']!='send_assistant_message':continue
                    args=SendAssistantMessageArguments.model_validate_json(call['function']['arguments'])
                    key='call:'+hashlib.sha256(call['id'].encode()).hexdigest()
                    await self._insert_delivery(cursor,record,key=key,recipient=args.recipient_userid,
                                                message=args.message,destination='assistant')
            await self.reminders.commit_actions(cursor,record,content)
            await self.portraits.commit_actions(cursor,record,content)
            now=utc_now()
            changes={'node_id':node_id,'updated_at':now,'processing_status':'running'}
            if status=='complete':
                finals=[call for message in content.exchanges if message.get('role')=='assistant'
                    for call in message.get('tool_calls',[]) if call['function']['name']=='finish_task']
                if len(finals)!=1:raise ValueError('助理最终结果必须对应唯一的 finish_task')
                final=FinishAssistantTaskArguments.model_validate_json(finals[0]['function']['arguments'])
                if final.content!=content.answer:raise ValueError('最终正文与 finish_task 不一致')
                if final.send_to_user:
                    await self._insert_delivery(cursor,record,key='final',recipient=record.userid,
                                                message=content.answer,destination='user')
                changes.update(processing_status='completed',finished_at=now,error_code=None)
            elif status!='generating':
                changes.update(processing_status='failed',finished_at=now,error_code='generation_interrupted')
            await self._save(cursor,record.model_copy(update=changes))
        return commit

    async def deliveries(self,task_id):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_deliveries WHERE task_id=%s ORDER BY created_at,id',(task_id,))
            return [_delivery(row) for row in await cursor.fetchall()]

    async def delivery_attempt(self,item,*,retry_after_seconds=None):
        # 先标记结果待核对。进程在请求中/成功回执保存前退出，机器人不会被自动重发。
        async with self.trees._transaction() as cursor:
            await cursor.execute("UPDATE assistant_deliveries SET status='failed',error_code='delivery_unknown',"
                "attempt_count=attempt_count+1,updated_at=%s,next_attempt_at=%s WHERE id=%s AND attempt_count=%s AND "
                "(status='pending' OR (status='failed' AND error_code='delivery_retryable') OR "
                "(destination='assistant' AND status='failed' AND error_code='delivery_unknown'))",
                (_date(utc_now()),_date(utc_now()+timedelta(seconds=retry_after_seconds)) if retry_after_seconds is not None else None,
                 item.id,item.attempt_count))
            return cursor.rowcount==1

    async def delivery_result(self,item,*,receipt=None,retryable=False):
        now=utc_now()
        async with self.trees._transaction() as cursor:
            if receipt is not None:
                await cursor.execute("UPDATE assistant_deliveries SET status='sent',error_code=NULL,sent_at=%s,"
                    'external_receipt=%s,updated_at=%s,next_attempt_at=NULL WHERE id=%s',(_date(now),_dump(receipt),_date(now),item.id))
            elif retryable:
                await cursor.execute("UPDATE assistant_deliveries SET error_code='delivery_retryable',updated_at=%s WHERE id=%s AND status<>'sent'",
                                     (_date(now),item.id))

    async def awaiting_ack(self,userid):
        async with self.trees._transaction() as cursor:
            await cursor.execute("SELECT * FROM assistant_task_executions WHERE userid=%s AND processing_status IN ('completed','failed') "
                'AND acknowledged_at IS NULL ORDER BY received_at,task_id',(userid,))
            return [_task(row) for row in await cursor.fetchall()]

    async def acknowledged(self,task_id):
        record=(await self.get_task(task_id)).record
        if record.processing_status not in ('completed','failed'):raise ValueError('不能确认未结束的助理任务')
        # 消费确认与业务成功分离：失败保留全部账本，未交付或结果不明的动作不再阻塞队列。
        if record.processing_status=='completed' and any(v.status!='sent' for v in await self.deliveries(task_id)):
            raise ValueError('不能确认尚未交付的任务')
        await self.update(task_id,acknowledged_at=record.acknowledged_at or utc_now())
        async with self.trees._transaction() as cursor:
            await cursor.execute('UPDATE assistant_task_executions SET delivery_token=NULL,lease_expires_at=NULL WHERE task_id=%s',(task_id,))
