"""中间件助理队列的精确 DTO；交付凭证不进入普通序列化或模型上下文。"""
from typing import Literal

from pydantic import AwareDatetime, Field, SecretStr, field_validator, model_validator

from app.model.assistant import AssistantModel, ExternalID, QueueTaskSnapshot, TaskType, UserID


class PublishAssistantTask(AssistantModel):
    userid: UserID
    task_type: TaskType
    message: str = Field(strict=True, min_length=1, max_length=10000, pattern=r'\S', repr=False)
    # 中间件允许省略，但本平台生产端统一要求稳定键，避免超时重试重复追加用户短消息。
    request_id: ExternalID
    source_assistant_id: ExternalID | None = Field(default=None, description='由可信任务宿主填写的发送方助理标识；assistant_message 必填，不能用接收用户代替。')

    @model_validator(mode='after')
    def require_assistant_source(self):
        if self.task_type == 'assistant_message' and self.source_assistant_id is None:
            raise ValueError('发布助理消息必须提供发送方助理标识')
        return self


class AssistantUpload(AssistantModel):
    file_extension: str = Field(strict=True, pattern=r'^[a-zA-Z0-9]{1,16}$')
    content: bytes = Field(strict=True, min_length=1, repr=False, exclude=True)

    @field_validator('file_extension')
    @classmethod
    def lower_extension(cls, value):
        return value.lower()


class AssistantPublished(AssistantModel):
    status: Literal['published']
    duplicate: bool = Field(strict=True)
    merged: bool = Field(strict=True)
    task: QueueTaskSnapshot = Field(repr=False)


class AssistantDeliveryLease(AssistantModel):
    task: QueueTaskSnapshot = Field(repr=False)
    delivery_token: SecretStr = Field(min_length=1, max_length=128, exclude=True, repr=False)
    lease_expires_at: AwareDatetime

    @field_validator('delivery_token')
    @classmethod
    def validate_token(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError('交付凭证不能为空')
        return value


class AssistantAcknowledged(AssistantModel):
    task_id: ExternalID
    status: Literal['acknowledged']
    duplicate: bool = Field(strict=True)


class AssistantQueueStatus(AssistantModel):
    userid: UserID
    has_in_progress_task: bool = Field(strict=True)
    task_id: ExternalID | None
    task_type: TaskType | None
    lease_expires_at: AwareDatetime | None
    last_heartbeat_at: AwareDatetime | None

    @model_validator(mode='after')
    def validate_status(self):
        fields = (self.task_id, self.task_type, self.lease_expires_at)
        if (self.has_in_progress_task and any(value is None for value in fields)
                or not self.has_in_progress_task and any(value is not None for value in fields)):
            raise ValueError('执行中标记与任务信息不一致')
        return self
