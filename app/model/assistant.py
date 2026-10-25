"""助理持久化契约；只验证数据，不启动消费者、调度器或读取实际配置。"""
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator
from app.model.question_file import AttachmentReference
from app.model.question_tree import TreeID, TreeModel, new_tree_id, utc_now

UserID = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'\S')]
ExternalID = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'\S')]
Text = Annotated[str, Field(strict=True, min_length=1, pattern=r'\S')]
TaskType = Literal['reminder', 'user_message', 'assistant_message', 'heartbeat']
ErrorCode = Annotated[str, Field(strict=True, pattern=r'^[a-zA-Z][a-zA-Z0-9_]{0,63}$')]


class AssistantModel(TreeModel):
    @field_validator('*')
    @classmethod
    def normalize_dates(cls, value):
        # 数据库存 UTC；不得把机器本地时区隐式用于提醒或原消息时间。
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('时间必须包含时区')
            return value.astimezone(timezone.utc)
        return value


class Assistant(AssistantModel):
    userid: UserID
    tree_id: TreeID
    tree_kind: Literal['assistant'] = 'assistant'
    enabled: bool = Field(default=True, strict=True)
    heartbeat_interval_seconds: int = Field(default=-1, strict=True, ge=-1, le=2147483647)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode='after')
    def validate_record(self):
        if self.heartbeat_interval_seconds == 0:
            raise ValueError('心跳间隔必须为 -1 或正整数')
        if self.updated_at < self.created_at:
            raise ValueError('更新时间不能早于创建时间')
        return self


class QueueFileReference(AssistantModel):
    file_id: str = Field(strict=True, pattern=r'^[A-Za-z0-9_-]{1,128}$')
    file_extension: str = Field(strict=True, pattern=r'^[a-z0-9]{1,16}$')


class QueueUserMessage(AssistantModel):
    message_id: ExternalID
    message: Text
    source_platform_code: str = Field(strict=True, min_length=1, max_length=64, pattern=r'\S')
    source_assistant_id: ExternalID | None = Field(default=None, description='本条原始消息的发送方助理标识；不是接收方 userid 或来源平台代码。')
    request_id: ExternalID | None = None
    created_at: AwareDatetime
    files: tuple[QueueFileReference, ...] = ()


class QueueTaskSnapshot(AssistantModel):
    """持久化 pull 的 task，不保存平台 token、delivery_token 或租约作为业务输入。"""
    task_id: ExternalID
    userid: UserID
    task_type: TaskType
    message: Text
    source_platform_code: str = Field(strict=True, min_length=1, max_length=64, pattern=r'\S')
    source_assistant_id: ExternalID | None = Field(default=None, description='发送方助理标识；assistant_message 必须提供，合并用户消息取首条来源。')
    request_id: ExternalID | None = None
    created_at: AwareDatetime
    sequence: str = Field(strict=True, pattern=r'^[1-9][0-9]*$')
    user_messages: tuple[QueueUserMessage, ...] = ()
    files: tuple[QueueFileReference, ...] = ()

    @model_validator(mode='after')
    def validate_group(self):
        # 接收方和认证平台均不能代替发送助理；缺失来源时拒绝执行，不能猜测回复目标。
        if self.task_type == 'assistant_message' and self.source_assistant_id is None:
            raise ValueError('助理消息必须提供发送方助理标识')
        if len({f.file_id for f in self.files}) != len(self.files):
            raise ValueError('任务附件引用重复')
        if self.task_type != 'user_message':
            if self.user_messages:
                raise ValueError('非用户任务不能携带合并用户消息')
            return self
        if not self.user_messages:
            raise ValueError('用户任务必须保留原始消息')
        if len({m.message_id for m in self.user_messages}) != len(self.user_messages):
            raise ValueError('原始消息标识重复')
        if tuple(f for m in self.user_messages for f in m.files) != self.files:
            raise ValueError('消息附件与任务汇总不一致')
        # Redis 入队顺序是权威顺序，不按时间重新排序；时钟回拨也不能改写消息顺序。
        if self.created_at != max(m.created_at for m in self.user_messages):
            raise ValueError('合并任务时间必须为最晚消息接收时间')
        first = self.user_messages[0]
        if (self.source_platform_code != first.source_platform_code or self.request_id != first.request_id
                or self.source_assistant_id != first.source_assistant_id):
            raise ValueError('任务来源必须保留首条消息的来源')
        return self


class TaskFileBinding(AssistantModel):
    """来源文件与本地 UUID 的持久映射；实际授权仍由树节点附件登记决定。"""
    source: QueueFileReference
    attachment: AttachmentReference


class AssistantTaskExecution(AssistantModel):
    task_id: ExternalID
    userid: UserID
    tree_id: TreeID
    node_id: TreeID | None = None
    task_type: TaskType
    input_payload: QueueTaskSnapshot
    file_bindings: tuple[TaskFileBinding, ...] = ()
    processing_status: Literal['received', 'prepared', 'running', 'completed', 'failed'] = 'received'
    error_code: ErrorCode | None = None
    received_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    finished_at: AwareDatetime | None = None
    acknowledged_at: AwareDatetime | None = None

    @model_validator(mode='after')
    def validate_record(self):
        snapshot = self.input_payload
        if (self.task_id, self.userid, self.task_type) != (snapshot.task_id, snapshot.userid, snapshot.task_type):
            raise ValueError('执行记录与冻结任务身份不一致')
        sources = {f.file_id: f for f in snapshot.files}
        seen = set()
        local_ids = set()
        for binding in self.file_bindings:
            key = binding.source.file_id
            if sources.get(key) != binding.source or key in seen or binding.attachment.file_id in local_ids:
                raise ValueError('本地附件映射重复或不属于冻结任务')
            seen.add(key)
            local_ids.add(binding.attachment.file_id)
        if self.processing_status in ('prepared', 'running', 'completed') and seen != set(sources):
            raise ValueError('任务附件尚未完整转存登记')
        if self.processing_status in ('running', 'completed') and self.node_id is None:
            raise ValueError('开始执行后必须绑定任务节点')
        terminal = self.processing_status in ('completed', 'failed')
        if terminal != (self.finished_at is not None):
            raise ValueError('处理终态与完成时间不一致')
        if self.updated_at < self.received_at:
            raise ValueError('更新时间不能早于接收时间')
        if self.finished_at is not None and not self.received_at <= self.finished_at <= self.updated_at:
            raise ValueError('完成时间超出记录时间范围')
        if self.acknowledged_at is not None and (not terminal or not self.finished_at <= self.acknowledged_at <= self.updated_at):
            raise ValueError('不能确认尚未结束的任务')
        return self

    @property
    def attachments(self) -> tuple[AttachmentReference, ...]:
        """供节点登记复用现有 TaskContent.attachments，不创造第二种模型附件格式。"""
        return tuple(binding.attachment for binding in self.file_bindings)

    def message_attachments(self, message_id: str) -> tuple[AttachmentReference, ...]:
        message = next((m for m in self.input_payload.user_messages if m.message_id == message_id), None)
        if message is None:
            raise ValueError('原始消息不存在')
        bound = {b.source.file_id: b.attachment for b in self.file_bindings}
        if any(f.file_id not in bound for f in message.files):
            raise ValueError('该消息的附件尚未完整转存')
        return tuple(bound[f.file_id] for f in message.files)


class OnceSchedule(AssistantModel):
    at: AwareDatetime


class IntervalSchedule(AssistantModel):
    start_at: AwareDatetime
    every_seconds: int = Field(strict=True, ge=1, le=2147483647)


class CalendarSchedule(AssistantModel):
    frequency: Literal['daily', 'weekly', 'monthly']
    start_date: date
    local_time: str = Field(strict=True, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$')
    weekdays: tuple[Annotated[int, Field(strict=True, ge=1, le=7)], ...] | None = None
    day_of_month: int | None = Field(default=None, strict=True, ge=1, le=31)
    missing_day: Literal['skip', 'last_day'] | None = None

    @model_validator(mode='after')
    def validate_calendar(self):
        if self.frequency == 'weekly':
            if not self.weekdays or len(set(self.weekdays)) != len(self.weekdays):
                raise ValueError('每周提醒必须指定不重复的星期，周一为 1')
        elif self.weekdays is not None:
            raise ValueError('只有每周提醒可以指定星期')
        if self.frequency == 'monthly':
            if self.day_of_month is None or self.missing_day is None:
                raise ValueError('每月提醒必须指定日期及缺失日期处理规则')
        elif self.day_of_month is not None or self.missing_day is not None:
            raise ValueError('只有每月提醒可以指定月内日期规则')
        return self


class AssistantReminder(AssistantModel):
    id: TreeID = Field(default_factory=new_tree_id)
    userid: UserID
    content: Text = Field(max_length=10000)
    schedule_type: Literal['once', 'interval', 'calendar']
    schedule_config: OnceSchedule | IntervalSchedule | CalendarSchedule
    timezone: str = Field(default='Asia/Shanghai', strict=True, min_length=1, max_length=64)
    next_fire_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    status: Literal['enabled', 'paused', 'completed', 'cancelled'] = 'enabled'
    created_by_task_id: ExternalID | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode='after')
    def validate_record(self):
        expected = {'once': OnceSchedule, 'interval': IntervalSchedule, 'calendar': CalendarSchedule}
        if not isinstance(self.schedule_config, expected[self.schedule_type]):
            raise ValueError('提醒类型与时间规则不一致')
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('提醒时区必须是有效 IANA 时区') from None
        if self.status == 'enabled' and self.next_fire_at is None:
            raise ValueError('启用提醒必须有下一次触发时间')
        if self.status in ('completed', 'cancelled') and self.next_fire_at is not None:
            raise ValueError('结束的提醒不能有下一次触发时间')
        if self.end_at is not None and self.next_fire_at is not None and self.next_fire_at > self.end_at:
            raise ValueError('下一次触发超过截止时间')
        if self.next_fire_at is not None:
            config = self.schedule_config
            if isinstance(config, OnceSchedule) and self.next_fire_at != config.at:
                raise ValueError('一次性提醒触发时间必须与规则一致')
            if isinstance(config, IntervalSchedule):
                delta = self.next_fire_at - config.start_at
                if delta < timedelta(0) or delta % timedelta(seconds=config.every_seconds):
                    raise ValueError('间隔提醒必须按起始时刻的固定周期推进')
            if isinstance(config, CalendarSchedule):
                local = self.next_fire_at.astimezone(ZoneInfo(self.timezone))
                if local.date() < config.start_date or local.strftime('%H:%M:%S') != config.local_time or local.microsecond:
                    raise ValueError('日历触发时间与开始日期或本地时刻不一致')
                if config.frequency == 'weekly' and local.isoweekday() not in config.weekdays:
                    raise ValueError('日历触发时间与星期不一致')
                if config.frequency == 'monthly':
                    from calendar import monthrange
                    last = monthrange(local.year, local.month)[1]
                    day = min(config.day_of_month, last) if config.missing_day == 'last_day' else config.day_of_month
                    if local.day != day:
                        raise ValueError('日历触发时间与月内日期不一致')
        if self.updated_at < self.created_at:
            raise ValueError('更新时间不能早于创建时间')
        return self


class AssistantDelivery(AssistantModel):
    id: TreeID = Field(default_factory=new_tree_id)
    userid: UserID
    task_id: ExternalID | None = None
    reminder_id: TreeID | None = None
    action_key: ExternalID
    scheduled_for: AwareDatetime | None = None
    destination: Literal['user', 'assistant']
    recipient_userid: UserID
    task_type: Literal['assistant_message', 'reminder'] | None = None
    message: Text
    status: Literal['pending', 'sent', 'failed'] = 'pending'
    attempt_count: int = Field(default=0, strict=True, ge=0, le=4294967295)
    next_attempt_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    external_receipt: dict[str, JsonValue] | None = None
    error_code: ErrorCode | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode='after')
    def validate_record(self):
        if (self.task_id is not None) == (self.reminder_id is not None):
            raise ValueError('投递必须且只能关联一个任务或提醒')
        if (self.reminder_id is not None) != (self.scheduled_for is not None):
            raise ValueError('提醒投递必须绑定本次计划触发时间')
        if (self.destination == 'user') != (self.task_type is None):
            raise ValueError('只有助理队列投递需要任务类型')
        if self.reminder_id is not None and (self.destination, self.task_type, self.recipient_userid) != ('assistant', 'reminder', self.userid):
            raise ValueError('到期提醒必须发给所属用户助理')
        if self.task_id is not None and self.destination == 'assistant' and self.task_type != 'assistant_message':
            raise ValueError('助理间沟通必须使用 assistant_message')
        if self.destination == 'assistant' and len(self.message) > 10000:
            raise ValueError('队列消息超过发布接口长度上限')
        if (self.status == 'sent') != (self.sent_at is not None):
            raise ValueError('投递状态与发送时间不一致')
        if self.status == 'sent' and self.next_attempt_at is not None:
            raise ValueError('已发送消息不能继续安排重试')
        if self.updated_at < self.created_at or (self.sent_at is not None and not self.created_at <= self.sent_at <= self.updated_at):
            raise ValueError('投递时间不一致')
        return self

    @property
    def request_id(self) -> str:
        # 同一持久化投递重试始终复用此键；不能每次 HTTP 调用重新生成。
        return f'assistant-delivery:{self.id}'
