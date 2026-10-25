"""助理专属提醒工具：查询、创建、删除；变更由节点事务钩子提交。"""
import logging
from datetime import date
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator
from app.agent.core.json_codec import validate_model_json
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult, FoldableToolContent, ToolPageReference
from app.agent.core.tool.result import ToolPageUnavailable
from app.model.assistant import OnceSchedule, IntervalSchedule, CalendarSchedule, AssistantReminder
from app.model.question_tree import TreeID, utc_now
from app.service.reminder_schedule import next_occurrence, describe_schedule

logger = logging.getLogger(__name__)


class OnceArguments(OnceSchedule):
    at: AwareDatetime = Field(strict=True, description='仅触发一次的准确时间，使用带 UTC 偏移的 ISO 8601，例如 2026-10-25T09:00:00+08:00；必须晚于当前时间。')


class IntervalArguments(IntervalSchedule):
    start_at: AwareDatetime = Field(strict=True, description='固定间隔的起始时刻，使用带 UTC 偏移的 ISO 8601；始终按此起点推进，不按实际执行完成时间顺延。')
    every_seconds: int = Field(strict=True, ge=1, le=2147483647, description='两次计划触发之间的正整数秒数，例如 7200 表示每两小时；每天固定当地时刻应使用 calendar。')


class CalendarArguments(CalendarSchedule):
    frequency: Literal['daily', 'weekly', 'monthly'] = Field(description='日历周期：daily 每天、weekly 每周指定星期、monthly 每月指定日期。')
    start_date: date = Field(strict=True, description='最早允许触发的当地日期，格式 YYYY-MM-DD；创建时不会补发此前错过的时刻。')
    local_time: str = Field(strict=True, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$', description='所选时区的当地时刻，格式 HH:MM:SS。夏令时缺失时刻跳过，重复时刻只触发第一次。')
    weekdays: tuple[Annotated[int, Field(strict=True, ge=1, le=7)], ...] | None = Field(default=None, description='仅 weekly 必填，星期一至日分别为 1 至 7，不能重复；其他周期省略。周一至周五不等于法定工作日。')
    day_of_month: int | None = Field(default=None, strict=True, ge=1, le=31, description='仅 monthly 必填，月内日期 1 至 31；其他周期省略。')
    missing_day: Literal['skip', 'last_day'] | None = Field(default=None, description='仅 monthly 必填：指定日期不存在时，skip 跳过该月，last_day 改为该月最后一天；其他周期省略。')

    @model_validator(mode='after')
    def strict_weekdays(self):
        if self.weekdays is not None and any(type(day) is not int or not 1 <= day <= 7 for day in self.weekdays):
            raise ValueError('星期必须是 1 至 7 的整数')
        return self


class CreateReminderArguments(WorkspaceObject):
    """当你需要让自己在未来某个时间检查事项或处理任务时，使用这个工具。支持一次性提醒、固定间隔提醒，以及每天、每周或每月的日历提醒；成功返回提醒 ID 和下一次触发时间。

    写清届时要做什么及必要背景，依据用户要求填写时间规则和时区。例如“每天上午九点检查待办”使用每天九点的日历规则，而不是每隔二十四小时。涉及相对时间时先确认当前时间；日期、周期或月末规则不明确时向用户澄清，不自行猜测。

    提醒到期后先由你处理，再通过 finish_task 决定是否通知用户；创建提醒不等于已经发送通知。设置失败时不能声称已安排。需要修改已有提醒时，先查看并删除原提醒，再创建新的提醒。
    """
    content: str = Field(strict=True, min_length=1, max_length=10000, pattern=r'\S', description='到期交给当前助理的任务正文，说明要检查或处理的事项及必要背景；不是直接发给用户的通知，不包含私有推理或访问凭据。')
    schedule_type: Literal['once', 'interval', 'calendar'] = Field(description='时间规则类型：once 一次性，interval 固定秒数间隔，calendar 按当地日历周期。必须与 schedule_config 对应。')
    schedule_config: OnceArguments | IntervalArguments | CalendarArguments = Field(description='对应类型的结构化时间规则；once 填 at，interval 填 start_at/every_seconds，calendar 填 frequency/start_date/local_time 及适用的星期或月内日期规则。')
    timezone: str = Field(default='Asia/Shanghai', strict=True, min_length=1, max_length=64, description='IANA 时区名称，默认 Asia/Shanghai；日历规则和展示按此时区解释，不能使用含糊的时区简称。')
    end_at: AwareDatetime | None = Field(default=None, description='可选的最后允许触发时刻，使用带 UTC 偏移的 ISO 8601，包含该时刻；不填表示周期提醒没有预设截止时间。')

    @model_validator(mode='after')
    def validate_rule(self):
        expected = {'once': OnceArguments, 'interval': IntervalArguments, 'calendar': CalendarArguments}
        if not isinstance(self.schedule_config, expected[self.schedule_type]):
            raise ValueError('提醒类型与时间规则不一致')
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('提醒时区不合法') from None
        return self


class ListRemindersArguments(WorkspaceObject):
    """当你需要了解自己有哪些尚未结束的提醒、避免重复设置，或找到要取消的提醒时，使用这个工具。查看提醒 ID、内容、时间规则、时区及下一次触发时间；例如用户要求取消明天的提醒时，先查看列表确认对象。

    首次从 offset=0 开始，之后使用返回的 next_offset 查看下一页；next_offset 为 null 表示已到末页。只返回当前助理的提醒，查询失败不能解释为没有提醒。
    """
    offset: int = Field(default=0, strict=True, ge=0, description='跳过的记录数量，首次查询为 0；继续查看使用上次结果的 next_offset，重新查询从 0 开始。')
    limit: int = Field(default=10, strict=True, ge=1, le=50, description='本页最多返回的提醒数量，默认 10，范围 1 至 50。')


class DeleteReminderArguments(WorkspaceObject):
    """当用户取消提醒，或需要替换已有提醒的时间规则时，使用这个工具。删除指定提醒并停止后续触发；例如取消每日待办提醒时，先从 list_reminders 找到对应提醒，再使用其准确 ID 删除。

    只能删除自己的提醒，不能凭内容猜测 ID。已经触发的提醒不会撤回，正在处理的任务也不会中断；成功只表示后续提醒已停止，失败时不能声称已经取消。
    """
    reminder_id: TreeID = Field(description='要删除的提醒 ID，原样使用 list_reminders 或 create_reminder 返回的 32 位小写十六进制标识；只能删除当前助理自己的提醒。')


def reminder_summary(reminder):
    return {'reminder_id': reminder.id, 'content': reminder.content,
            'schedule': describe_schedule(reminder.schedule_config, reminder.timezone),
            'timezone': reminder.timezone,
            'next_fire_at': reminder.next_fire_at.astimezone(ZoneInfo(reminder.timezone)).isoformat() if reminder.next_fire_at else None,
            'end_at': reminder.end_at.astimezone(ZoneInfo(reminder.timezone)).isoformat() if reminder.end_at else None,
            'status': reminder.status}


def render_reminders(rows):
    lines = ['# 当前定时任务']
    for row in rows:
        info = reminder_summary(row)
        lines.extend(['', '---', '', f'提醒 ID：{row.id}', '', '提醒内容：', '', row.content, '',
                      '时间规则：' + info['schedule'], '时区：' + info['timezone'],
                      '下次触发：' + (info['next_fire_at'] or '暂无'),
                      '截止时间：' + (info['end_at'] or '未设置'),
                      '状态：' + ('启用' if row.status == 'enabled' else '暂停')])
    return '\n'.join(lines)


class ReminderTools:
    """每任务独立的资料页面；写工具仅预验，权威变更交给同事务提交钩子。"""
    def __init__(self, repository, userid, *, clock=utc_now):
        self.repository, self.userid, self.clock = repository, userid, clock
        self._page = None

    def close(self):
        self._page = None

    async def create(self, args):
        next_at = next_occurrence(args.schedule_config, args.timezone, self.clock(), end_at=args.end_at)
        if next_at is None:
            return ToolExecutionResult(status='failed', tool_result={'code':'no_future_occurrence', 'message':'该规则没有未来触发时间，请核对当前时间、规则和截止时间；本次未创建提醒。'})
        # ID 和首次排期由程序生成；不向模型索取身份、下一次时间或事务标识。
        reminder = AssistantReminder(userid=self.userid, **args.model_dump(), next_fire_at=next_at)
        return ToolExecutionResult(status='succeeded', tool_result={'message':'提醒已创建。', **reminder_summary(reminder)})

    async def delete(self, args):
        row = await self.repository.find(self.userid, args.reminder_id)
        if row is None:
            return ToolExecutionResult(status='failed', tool_result={'code':'reminder_not_found', 'message':'未找到当前助理的该提醒，请重新查看列表。'})
        return ToolExecutionResult(status='succeeded', tool_result={
            'reminder_id': row.id, 'message':'提醒已删除，后续不再触发；已经生成的交付或入队任务不会撤回。'})

    async def list(self, args):
        rows = await self.repository.list_current(self.userid, offset=args.offset, limit=args.limit + 1)
        more = len(rows) > args.limit
        rows = rows[:args.limit]
        result = {'count':len(rows), 'next_offset':args.offset + len(rows) if more else None,
                  'message':'已读取本页定时任务。' if rows else '当前页没有定时任务。'}
        if not rows:
            return ToolExecution(ToolExecutionResult(status='succeeded', tool_result=result), after_commit=self.close)
        ref = ToolPageReference(resource_id='reminders:' + uuid4().hex, display_id=uuid4().hex,
                                locator=f'offset:{args.offset}', media_type='text', description='本次查询的当前定时任务')
        text = render_reminders(rows)
        def accept():
            self._page = (ref, text)
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result=result,
            content=FoldableToolContent(pages=[ref])), after_commit=accept)

    async def resolve_page(self, ref):
        if self._page is None or self._page[0] != ref:
            raise ToolPageUnavailable('这次提醒列表已不可读取，需要时请重新查询。')
        return [{'type':'text', 'text':self._page[1]}]

    def registrations(self):
        def safe(method):
            async def execute(args):
                try:
                    return await method(args)
                except Exception as error:
                    logger.warning('提醒工具预验失败：error_type=%s', type(error).__name__)
                    return ToolExecutionResult(status='failed', tool_result={
                        'code':'reminder_unavailable', 'message':'提醒资料暂不可用，本次操作未提交，不能据此判断列表为空或提醒已设置、删除。'})
            return execute
        return tuple(RegisteredTool(name, args.__doc__, args,
            lambda name, raw, workspace, arguments: workspace, executor=safe(method),
            parser=(lambda raw: validate_model_json(CreateReminderArguments, raw)) if name == 'create_reminder' else None,
            **({'return_types':('ordinary','foldable'), 'page_resolver':self.resolve_page} if name == 'list_reminders' else {}))
            for name, args, method in (
                ('list_reminders', ListRemindersArguments, self.list),
                ('create_reminder', CreateReminderArguments, self.create),
                ('delete_reminder', DeleteReminderArguments, self.delete)))
