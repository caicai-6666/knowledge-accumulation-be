"""提醒查询、工具事务提交和到期排期；不在事务内调用中间件。"""
from datetime import datetime, timedelta, timezone
import json

from app.model.assistant import AssistantReminder, AssistantDelivery
from app.model.question_tree import utc_now
from app.repository.question_tree import _date, _decode, _dump
from app.service.reminder_schedule import next_occurrence


def restore_reminder(row):
    values = {key: value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) else value for key, value in row.items()}
    values['schedule_config'] = _decode(values['schedule_config'])
    return AssistantReminder.model_validate(values)


class MySQLReminderRepository:
    def __init__(self, trees):
        self.trees = trees

    async def validate_schema(self):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT id,userid,content,schedule_type,schedule_config,timezone,next_fire_at,end_at,status '
                                 'FROM assistant_reminders LIMIT 0')

    async def find(self, userid, reminder_id):
        async with self.trees._transaction() as cursor:
            await cursor.execute('SELECT * FROM assistant_reminders WHERE id=%s AND userid=%s', (reminder_id, userid))
            row = await cursor.fetchone()
            return restore_reminder(row) if row else None

    async def list_current(self, userid, *, offset, limit):
        async with self.trees._transaction() as cursor:
            await cursor.execute("SELECT * FROM assistant_reminders WHERE userid=%s AND status IN ('enabled','paused') "
                                 'ORDER BY created_at,id LIMIT %s OFFSET %s', (userid, limit, offset))
            return [restore_reminder(row) for row in await cursor.fetchall()]

    async def commit_actions(self, cursor, record, content):
        # 只消费节点已接受的工具对；失败轨迹不在 exchanges，不能产生任何提醒写入。
        from app.agent.assistant.tool.reminder import CreateReminderArguments, DeleteReminderArguments
        receipts = {message['tool_call_id']: message['content'] for message in content.exchanges if message.get('role') == 'tool'}
        for message in content.exchanges:
            if message.get('role') != 'assistant':
                continue
            for call in message.get('tool_calls', []):
                name = call['function']['name']
                if name == 'create_reminder':
                    args = CreateReminderArguments.model_validate_json(call['function']['arguments'])
                    receipt = json.loads(receipts[call['id']])
                    reminder = AssistantReminder(userid=record.userid, created_by_task_id=record.task_id,
                        id=receipt['reminder_id'], next_fire_at=receipt['next_fire_at'], **args.model_dump())
                    await self._create(cursor, reminder)
                elif name == 'delete_reminder':
                    args = DeleteReminderArguments.model_validate_json(call['function']['arguments'])
                    await self._delete(cursor, record.userid, args.reminder_id)

    async def _create(self, cursor, reminder):
        await cursor.execute('SELECT * FROM assistant_reminders WHERE id=%s FOR UPDATE', (reminder.id,))
        row = await cursor.fetchone()
        if row:
            old = restore_reminder(row)
            immutable = ('userid', 'content', 'schedule_type', 'schedule_config', 'timezone', 'end_at', 'created_by_task_id')
            if any(getattr(old, name) != getattr(reminder, name) for name in immutable):
                raise ValueError('提醒标识对应不同的创建动作')
            # 后续检查点重放已接受动作时，不能重置已推进、完成或删除的提醒。
            return
        await cursor.execute('INSERT INTO assistant_reminders '
            '(id,userid,content,schedule_type,schedule_config,timezone,next_fire_at,end_at,status,created_by_task_id,created_at,updated_at) '
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'enabled',%s,%s,%s)",
            (reminder.id, reminder.userid, reminder.content, reminder.schedule_type,
             _dump(reminder.schedule_config.model_dump(mode='json')), reminder.timezone,
             _date(reminder.next_fire_at), _date(reminder.end_at) if reminder.end_at else None,
             reminder.created_by_task_id, _date(reminder.created_at), _date(reminder.updated_at)))

    async def _delete(self, cursor, userid, reminder_id):
        await cursor.execute('SELECT * FROM assistant_reminders WHERE id=%s AND userid=%s FOR UPDATE', (reminder_id, userid))
        row = await cursor.fetchone()
        if row is None:
            raise ValueError('提醒不属于当前助理或已不存在')
        if row['status'] != 'cancelled':
            # 保留外键引用及已产生的交付事实；删除仅关闭未来排期，不撤回已经冻结的任务。
            await cursor.execute("UPDATE assistant_reminders SET status='cancelled',next_fire_at=NULL,updated_at=%s WHERE id=%s",
                                 (_date(utc_now()), reminder_id))

    async def materialize_due(self, *, now, limit):
        """竞争同一提醒行锁：本次发生记录与未来排期一起提交，多实例不会重复物化。"""
        count = 0
        async with self.trees._transaction() as cursor:
            await cursor.execute("SELECT * FROM assistant_reminders WHERE status='enabled' AND next_fire_at<=%s "
                'AND userid IN (SELECT userid FROM assistants WHERE enabled=1) '
                'ORDER BY next_fire_at,id LIMIT %s FOR UPDATE SKIP LOCKED', (_date(now), limit))
            for row in await cursor.fetchall():
                reminder = restore_reminder(row)
                item = AssistantDelivery(userid=reminder.userid, reminder_id=reminder.id,
                    action_key='occurrence', scheduled_for=reminder.next_fire_at,
                    destination='assistant', recipient_userid=reminder.userid, task_type='reminder', message=reminder.content)
                await cursor.execute('INSERT INTO assistant_deliveries '
                    '(id,userid,reminder_id,action_key,scheduled_for,destination,recipient_userid,task_type,message,status,attempt_count,created_at,updated_at) '
                    "VALUES(%s,%s,%s,%s,%s,'assistant',%s,'reminder',%s,'pending',0,%s,%s)",
                    (item.id, item.userid, item.reminder_id, item.action_key, _date(item.scheduled_for),
                     item.recipient_userid, item.message, _date(item.created_at), _date(item.updated_at)))
                # 停机漏过多个周期只合并一次；按原规则定位未来时刻，不按消息发送时间漂移。
                future = next_occurrence(reminder.schedule_config, reminder.timezone, now, end_at=reminder.end_at)
                await cursor.execute('UPDATE assistant_reminders SET next_fire_at=%s,status=%s,updated_at=%s WHERE id=%s',
                    (_date(future) if future else None, 'enabled' if future else 'completed', _date(utc_now()), reminder.id))
                count += 1
        return count

    async def pending_deliveries(self, *, now, limit, retry_window_seconds):
        from .assistant_runtime import _delivery
        async with self.trees._transaction() as cursor:
            await cursor.execute("SELECT * FROM assistant_deliveries WHERE reminder_id IS NOT NULL "
                "AND (status='pending' OR (status='failed' AND error_code='delivery_retryable') OR "
                "(status='failed' AND error_code='delivery_unknown' AND created_at>=%s)) "
                'AND (next_attempt_at IS NULL OR next_attempt_at<=%s) '
                'AND userid IN (SELECT userid FROM assistants WHERE enabled=1) '
                'ORDER BY next_attempt_at,created_at,id LIMIT %s',
                (_date(now - timedelta(seconds=retry_window_seconds)), _date(now), limit))
            return [_delivery(row) for row in await cursor.fetchall()]
