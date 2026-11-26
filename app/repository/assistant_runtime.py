"""助理绑定、执行账本、交付记录与助理树附件登记；脱离旧 question_trees 外键。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from pydantic import SecretStr

from app.model.assistant import Assistant, AssistantDelivery, AssistantTaskExecution, TaskFileBinding
from app.model.assistant_queue import AssistantDeliveryLease
from app.model.question_file import AttachmentReference
from app.model.question_tree import new_tree_id, utc_now
from app.repository.assistant_portrait import AssistantPortraitRepository
from app.repository.mysql_base import MySQLRepositoryBase, _date, _decode, _dump, _row


def task_node_id(task_id: str) -> str:
    return uuid5(NAMESPACE_URL, 'knowledge-assistant-task:' + task_id).hex


@dataclass(frozen=True)
class StoredAssistantTask:
    record: AssistantTaskExecution
    lease: AssistantDeliveryLease | None


def _task(row) -> StoredAssistantTask:
    value = {
        key: item.replace(tzinfo=timezone.utc) if isinstance(item, datetime) else item
        for key, item in row.items()
    }
    token = value.pop('delivery_token')
    expiry = value.pop('lease_expires_at')
    value['input_payload'] = _decode(value['input_payload'])
    value['file_bindings'] = _decode(value['file_bindings'])
    record = _row(AssistantTaskExecution, value)
    lease = (
        None
        if token is None
        else AssistantDeliveryLease(
            task=record.input_payload,
            delivery_token=SecretStr(token),
            lease_expires_at=expiry.replace(tzinfo=record.received_at.tzinfo),
        )
    )
    return StoredAssistantTask(record, lease)


def _delivery(row) -> AssistantDelivery:
    value = {
        key: item.replace(tzinfo=timezone.utc) if isinstance(item, datetime) else item
        for key, item in row.items()
    }
    if value['external_receipt'] is not None:
        value['external_receipt'] = _decode(value['external_receipt'])
    return _row(AssistantDelivery, value)


class MySQLAssistantRepository:
    """个人助理核心账本仓库（MySQL）：管理 assistants / assistant_task_executions / assistant_deliveries / assistant_tree_files。"""

    def __init__(self, database: MySQLRepositoryBase) -> None:
        self.database = database
        # 兼容历史调用方的 `.trees` 属性引用（如 RobotInboxRepository）。
        self.trees = database
        self.portraits = AssistantPortraitRepository(database)

    async def validate_schema(self) -> None:
        await self.portraits.validate_schema()
        async with self.database._transaction() as cursor:
            await cursor.execute('SELECT userid,tree_id,enabled FROM assistants LIMIT 0')
            await cursor.execute('SELECT task_id,delivery_token,lease_expires_at FROM assistant_task_executions LIMIT 0')
            await cursor.execute('SELECT id,action_key,status,error_code,file_id FROM assistant_deliveries LIMIT 0')
            await cursor.execute("SHOW INDEX FROM assistant_task_executions WHERE Key_name='uq_assistant_active'")
            if not await cursor.fetchone():
                raise ValueError('请先执行 012 助理消费迁移')

    def user_scope(self, userid: str, *, namespace: str = 'assistant'):
        return self.database.user_scope(userid, namespace=namespace)

    async def ensure(self, userid: str) -> Assistant:
        async with self.database._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistants WHERE userid=%s', (userid,))
            row = await cursor.fetchone()
            if row:
                row = dict(row)
                row['enabled'] = bool(row['enabled'])
                return _row(Assistant, row)
            assistant = Assistant(userid=userid, tree_id=new_tree_id())
            await cursor.execute(
                'INSERT INTO assistants(userid,tree_id,tree_kind,enabled,created_at,updated_at) '
                'VALUES(%s,%s,%s,1,%s,%s)',
                (
                    userid,
                    assistant.tree_id,
                    'assistant',
                    _date(assistant.created_at),
                    _date(assistant.updated_at),
                ),
            )
            return assistant

    async def list_enabled_assistants(self) -> list[Assistant]:
        """列出所有已启用助理，供 1B 全局后台巡检驱动 AgentScheduler.tick()。"""
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'SELECT userid,tree_id,tree_kind,enabled,created_at,updated_at '
                'FROM assistants WHERE enabled=1 ORDER BY userid'
            )
            rows = await cursor.fetchall()
        result: list[Assistant] = []
        for raw in rows:
            row = dict(raw)
            row['enabled'] = bool(row['enabled'])
            result.append(_row(Assistant, row))
        return result

    async def recipient_enabled(self, userid: str) -> bool:
        async with self.database._transaction() as cursor:
            await cursor.execute('SELECT enabled FROM assistants WHERE userid=%s', (userid,))
            row = await cursor.fetchone()
            # 尚未创建绑定的真实员工可接收，消费者随后会创建其唯一助理。
            return row is None or bool(row['enabled'])

    async def register_file(
        self,
        tree_id: str,
        owner_userid: str,
        reference: AttachmentReference,
    ) -> None:
        """登记助理树内已落盘附件，供 view_pdf、文件交付与引用复用核对归属。"""
        now = utc_now()
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'INSERT IGNORE INTO assistant_tree_files(tree_id,owner_userid,file_id,file_type,created_at) '
                'VALUES(%s,%s,%s,%s,%s)',
                (tree_id, owner_userid, reference.file_id, reference.type, _date(now)),
            )

    async def find_file(self, tree_id: str, owner_userid: str, file_id: str) -> AttachmentReference:
        """按树与所属用户核对附件登记；同时兼容从历史 task file_bindings 中回查。"""
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'SELECT file_type FROM assistant_tree_files '
                'WHERE tree_id=%s AND owner_userid=%s AND file_id=%s',
                (tree_id, owner_userid, file_id),
            )
            row = await cursor.fetchone()
            if row is not None:
                return AttachmentReference(type=row['file_type'], file_id=file_id)
            await cursor.execute(
                'SELECT file_bindings FROM assistant_task_executions '
                'WHERE tree_id=%s AND userid=%s ORDER BY received_at DESC LIMIT 200',
                (tree_id, owner_userid),
            )
            for task_row in await cursor.fetchall():
                for item in _decode(task_row['file_bindings']) or ():
                    attachment = item.get('attachment') if isinstance(item, dict) else None
                    if isinstance(attachment, dict) and attachment.get('file_id') == file_id:
                        return AttachmentReference.model_validate(attachment)
        raise LookupError('助理树附件不存在')

    async def has_file(self, tree_id: str, owner_userid: str, file_id: str) -> bool:
        try:
            await self.find_file(tree_id, owner_userid, file_id)
            return True
        except LookupError:
            return False

    async def find_message_content(self, userid: str, request_id: str, *, platform_code: str):
        """按可信入站关联查找原消息的正文和完整附件，不能把外部消息 ID 当作文件 ID。"""
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'SELECT e.* FROM assistant_task_executions AS e '
                'JOIN assistants AS a ON a.userid=e.userid AND a.tree_id=e.tree_id '
                "WHERE e.userid=%s AND JSON_CONTAINS(e.input_payload,%s,'$.user_messages')=1",
                (userid, _dump([{'request_id': request_id, 'source_platform_code': platform_code}])),
            )
            found = []
            for row in await cursor.fetchall():
                record = _task(row).record
                for message in record.input_payload.user_messages:
                    if message.request_id == request_id and message.source_platform_code == platform_code:
                        refs = record.message_attachments(message.message_id)
                        if len(refs) == len(message.files) and all(ref.type == 'images' for ref in refs):
                            found.append((record.tree_id, message.message, refs))
            return found[0] if len(found) == 1 else None

    async def find_message_attachment(self, userid: str, request_id: str, *, platform_code: str):
        found = await self.find_message_content(userid, request_id, platform_code=platform_code)
        if found is None or len(found[2]) != 1:
            return None
        return found[0], found[2][0]

    async def get_task(self, task_id: str) -> StoredAssistantTask | None:
        async with self.database._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_task_executions WHERE task_id=%s', (task_id,))
            row = await cursor.fetchone()
            return _task(row) if row else None

    async def receive(self, assistant: Assistant, lease: AssistantDeliveryLease) -> StoredAssistantTask:
        if assistant.userid != lease.task.userid:
            raise ValueError('领取任务不属于当前助理')
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'SELECT * FROM assistant_task_executions WHERE task_id=%s FOR UPDATE',
                (lease.task.task_id,),
            )
            row = await cursor.fetchone()
            if row:
                stored = _task(row)
                if stored.record.input_payload != lease.task or stored.record.tree_id != assistant.tree_id:
                    raise ValueError('重复领取对应不同的冻结任务或助理树')
            else:
                now = utc_now()
                await cursor.execute(
                    'INSERT INTO assistant_task_executions '
                    '(task_id,userid,tree_id,task_type,input_payload,file_bindings,processing_status,received_at,updated_at) '
                    "VALUES(%s,%s,%s,%s,%s,'[]','received',%s,%s)",
                    (
                        lease.task.task_id,
                        assistant.userid,
                        assistant.tree_id,
                        lease.task.task_type,
                        _dump(lease.task.model_dump(mode='json')),
                        _date(now),
                        _date(now),
                    ),
                )
            await cursor.execute(
                'UPDATE assistant_task_executions SET delivery_token=%s,lease_expires_at=%s WHERE task_id=%s',
                (
                    lease.delivery_token.get_secret_value(),
                    _date(lease.lease_expires_at),
                    lease.task.task_id,
                ),
            )
        stored = await self.get_task(lease.task.task_id)
        assert stored is not None
        return stored

    async def _save(self, cursor, record: AssistantTaskExecution) -> None:
        record = AssistantTaskExecution.model_validate(record.model_dump())
        await cursor.execute(
            'UPDATE assistant_task_executions SET node_id=%s,file_bindings=%s,processing_status=%s,error_code=%s,'
            'updated_at=%s,finished_at=%s,acknowledged_at=%s WHERE task_id=%s',
            (
                record.node_id,
                _dump([v.model_dump(mode='json') for v in record.file_bindings]),
                record.processing_status,
                record.error_code,
                _date(record.updated_at),
                _date(record.finished_at) if record.finished_at else None,
                _date(record.acknowledged_at) if record.acknowledged_at else None,
                record.task_id,
            ),
        )

    async def update(self, task_id: str, **changes) -> AssistantTaskExecution:
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'SELECT * FROM assistant_task_executions WHERE task_id=%s FOR UPDATE',
                (task_id,),
            )
            record = _task(await cursor.fetchone()).record
            await self._save(cursor, record.model_copy(update={**changes, 'updated_at': utc_now()}))
        stored = await self.get_task(task_id)
        assert stored is not None
        return stored.record

    async def bind_file(self, task_id: str, binding: TaskFileBinding) -> AssistantTaskExecution:
        stored = await self.get_task(task_id)
        assert stored is not None
        current = [v for v in stored.record.file_bindings if v.source == binding.source]
        if current:
            if current[0] != binding:
                raise ValueError('附件映射不能覆盖')
            return stored.record
        await self.register_file(stored.record.tree_id, stored.record.userid, binding.attachment)
        return await self.update(task_id, file_bindings=(*stored.record.file_bindings, binding))

    async def record_delivery(
        self,
        *,
        userid: str,
        task_id: str,
        action_key: str,
        recipient_userid: str,
        message: str,
        destination: str,
        file_id: str | None = None,
    ) -> AssistantDelivery:
        """幂等写入 assistant_deliveries 交付记录；用于工具执行期与 finish_task 最终交付。"""
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'SELECT * FROM assistant_deliveries WHERE task_id=%s AND action_key=%s',
                (task_id, action_key),
            )
            row = await cursor.fetchone()
            if row:
                existing = _delivery(row)
                if (existing.recipient_userid, existing.message, existing.destination, existing.file_id) != (
                    recipient_userid,
                    message,
                    destination,
                    file_id,
                ):
                    raise ValueError('已接受发送动作不能覆盖')
                return existing
            item = AssistantDelivery(
                userid=userid,
                task_id=task_id,
                action_key=action_key,
                destination=destination,
                recipient_userid=recipient_userid,
                message=message,
                file_id=file_id,
                task_type='assistant_message' if destination == 'assistant' else None,
            )
            await cursor.execute(
                'INSERT INTO assistant_deliveries '
                '(id,userid,task_id,action_key,destination,recipient_userid,task_type,message,file_id,status,attempt_count,created_at,updated_at) '
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',0,%s,%s)",
                (
                    item.id,
                    item.userid,
                    item.task_id,
                    item.action_key,
                    item.destination,
                    item.recipient_userid,
                    item.task_type,
                    item.message,
                    item.file_id,
                    _date(item.created_at),
                    _date(item.updated_at),
                ),
            )
            return item

    async def deliveries(self, task_id: str) -> list[AssistantDelivery]:
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'SELECT * FROM assistant_deliveries WHERE task_id=%s ORDER BY created_at,id',
                (task_id,),
            )
            return [_delivery(row) for row in await cursor.fetchall()]

    async def delivery_attempt(self, item: AssistantDelivery, *, retry_after_seconds: int | None = None) -> bool:
        # 先标记结果待核对。进程在请求中/成功回执保存前退出，机器人不会被自动重发。
        async with self.database._transaction() as cursor:
            await cursor.execute(
                "UPDATE assistant_deliveries SET status='failed',error_code='delivery_unknown',"
                'attempt_count=attempt_count+1,updated_at=%s,next_attempt_at=%s WHERE id=%s AND attempt_count=%s AND '
                "(status='pending' OR (status='failed' AND error_code='delivery_retryable') OR "
                "(destination='assistant' AND status='failed' AND error_code='delivery_unknown'))",
                (
                    _date(utc_now()),
                    _date(utc_now() + timedelta(seconds=retry_after_seconds))
                    if retry_after_seconds is not None
                    else None,
                    item.id,
                    item.attempt_count,
                ),
            )
            return cursor.rowcount == 1

    async def delivery_result(
        self,
        item: AssistantDelivery,
        *,
        receipt: dict | None = None,
        retryable: bool = False,
    ) -> None:
        now = utc_now()
        async with self.database._transaction() as cursor:
            if receipt is not None:
                await cursor.execute(
                    "UPDATE assistant_deliveries SET status='sent',error_code=NULL,sent_at=%s,"
                    'external_receipt=%s,updated_at=%s,next_attempt_at=NULL WHERE id=%s',
                    (_date(now), _dump(receipt), _date(now), item.id),
                )
            elif retryable:
                await cursor.execute(
                    "UPDATE assistant_deliveries SET error_code='delivery_retryable',updated_at=%s "
                    "WHERE id=%s AND status<>'sent'",
                    (_date(now), item.id),
                )

    async def awaiting_ack(self, userid: str) -> list[StoredAssistantTask]:
        async with self.database._transaction() as cursor:
            await cursor.execute(
                "SELECT * FROM assistant_task_executions WHERE userid=%s AND processing_status IN ('completed','failed') "
                'AND acknowledged_at IS NULL ORDER BY received_at,task_id',
                (userid,),
            )
            return [_task(row) for row in await cursor.fetchall()]

    async def acknowledged(self, task_id: str) -> None:
        stored = await self.get_task(task_id)
        assert stored is not None
        record = stored.record
        if record.processing_status not in ('completed', 'failed'):
            raise ValueError('不能确认未结束的助理任务')
        # 消费确认与业务成功分离：失败保留全部账本，未交付或结果不明的动作不再阻塞队列。
        if record.processing_status == 'completed' and any(
            v.status != 'sent' for v in await self.deliveries(task_id)
        ):
            raise ValueError('不能确认尚未交付的任务')
        await self.update(task_id, acknowledged_at=record.acknowledged_at or utc_now())
        async with self.database._transaction() as cursor:
            await cursor.execute(
                'UPDATE assistant_task_executions SET delivery_token=NULL,lease_expires_at=NULL WHERE task_id=%s',
                (task_id,),
            )
