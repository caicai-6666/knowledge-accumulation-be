"""跨平台请求和反馈的严格契约；交付凭证只在传输层驻留。"""
from typing import Annotated, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, model_validator

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'\S')]
PlatformCode = Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')]
Message = Annotated[str, Field(strict=True, min_length=1, max_length=10000, pattern=r'\S')]


class A2AModel(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class PlatformA2AStateError(ValueError):
    """仅传递状态校验分支提供的安全反馈，不回显外部异常或请求正文。"""

    def __init__(self, reason, required_action):
        super().__init__(reason)
        self.reason, self.required_action = reason, required_action


class A2AWait(A2AModel):
    """本地等待记录，绝不作为中间件发布请求；关联信息只留在私有账本。"""
    broadcast_id: Identifier
    userid: Identifier
    response_id: Identifier
    response_platform_code: PlatformCode
    wait_for: Identifier


class A2APublish(A2AModel):
    broadcast_id: Identifier
    userid: Identifier
    message: Message
    target_platform_codes: list[PlatformCode] | None = None
    response_id: Identifier | None = None
    response_platform_code: PlatformCode | None = None

    @model_validator(mode='after')
    def check_target(self):
        if (self.response_id is None) != (self.response_platform_code is None):
            raise ValueError('续聊标识必须成对提供')
        if (self.target_platform_codes is not None) == (self.response_id is not None):
            raise ValueError('首次请求与续聊必须二选一')
        if self.target_platform_codes is not None and (not 1 <= len(self.target_platform_codes) <= 100
                or len(set(self.target_platform_codes)) != len(self.target_platform_codes)):
            raise ValueError('目标平台应为 1—100 个且不能重复')
        return self


class A2ATaskReference(A2AModel):
    task_id: Identifier
    target_platform_code: PlatformCode


class A2APublished(A2AModel):
    status: Literal['published']
    duplicate: bool = Field(strict=True)
    broadcast_id: Identifier
    userid: Identifier
    source_platform_code: Literal['knowledge-accumulation']
    target_platform_codes: list[PlatformCode]
    created_at: AwareDatetime
    tasks: list[A2ATaskReference]
    reply_flow: Literal['broadcast', 'direct']


class A2AReply(A2AModel):
    message_id: Identifier
    task_id: Identifier
    broadcast_id: Identifier
    userid: Identifier
    platform_code: PlatformCode
    # 与中间件终态枚举一致；不受理必须由明确状态表达，不从失败文本推断。
    status: Literal['succeeded', 'not_applicable', 'context_expired', 'failed']
    response_id: Identifier | None
    message: Message
    created_at: AwareDatetime

    @model_validator(mode='after')
    def check_rejection(self):
        if self.status == 'not_applicable' and self.response_id is not None:
            raise ValueError('不受理反馈不能产生续聊上下文')
        return self


class A2ABroadcast(A2AModel):
    broadcast_id: Identifier
    userid: Identifier
    state: Literal['open', 'closed']
    target_platform_codes: list[PlatformCode]
    pending_platform_codes: list[PlatformCode]
    replies: list[A2AReply]


class A2AClosed(A2AModel):
    broadcast_id: Identifier
    state: Literal['closed']
    duplicate: bool = Field(strict=True)
    closed_at: AwareDatetime


class A2ADelivery(A2AModel):
    message: A2AReply
    delivery_token: SecretStr = Field(exclude=True, repr=False, min_length=1)
    lease_expires_at: AwareDatetime


class A2ADirectWindow(A2AModel):
    duplicate: bool = Field(strict=True)
    capacity: int = Field(strict=True, ge=1, le=100)
    inflight_count: int = Field(strict=True, ge=0)
    available_slots: int = Field(strict=True, ge=0)
    messages: list[A2ADelivery]
    renewed_messages: list[A2ADelivery]


class A2AAcknowledged(A2AModel):
    message_id: Identifier
    status: Literal['acknowledged']
    duplicate: bool = Field(strict=True)
    acknowledged_at: AwareDatetime
