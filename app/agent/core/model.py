"""Agent 内部调用契约；不将工程身份加入模型上下文。"""
from datetime import datetime, timezone
from typing import Annotated, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field,JsonValue,field_validator
from app.auth.model import AuthenticatedUser
from app.model.personnel import PlatformCode
from app.model.question_file import AttachmentReference,unique_references
from .workspace import WorkspacePayload, empty_workspace


class CoreModel(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)


class HistoryTurn(CoreModel):
    created_at: AwareDatetime | None = Field(default=None, description="历史任务的原始创建时间；未知时为空，不以当前时间代替。")
    question: str = Field(strict=True,min_length=1)
    attachments: list[AttachmentReference] = Field(default_factory=list)
    intermediate: list[str] = Field(default_factory=list)
    answer: str = Field(default='',strict=True)
    status: Literal['complete','cancelled','superseded','failed'] = 'complete'
    # 只接收权威已接受工具对；未提供时仅使用明确的公开输出，不反推工具调用。
    native_messages: list[dict[str,JsonValue]] | None = None

    _unique_attachments=field_validator('attachments')(unique_references)

    @field_validator('native_messages')
    @classmethod
    def validate_native(cls,value):
        if value is None:return None
        from .native_messages import validate_native_messages
        return validate_native_messages(value)


class ContextInjection(CoreModel):
    summary: str | None = None
    turns: list[HistoryTurn] = Field(default_factory=list)
    workspace: WorkspacePayload = Field(default_factory=empty_workspace)
    supplemental: str = Field(default='',strict=True)


class AgentInput(CoreModel):
    # 独立Core输入只采集一次；持久化宿主在执行前绑定实际节点时间。
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc),
        description="本次任务创建时间，由宿主绑定；用于相对日期，不是业务事件发生时间。")
    question: str = Field(strict=True,min_length=1,pattern=r'\S')
    attachments: list[AttachmentReference] = Field(default_factory=list,description='当前用户任务的附件引用，按输入顺序展示。')
    context: ContextInjection = Field(default_factory=ContextInjection)
    thinking_effort: Literal['none','low','medium','xhigh'] = 'xhigh'
    platform_permissions: dict[PlatformCode,Annotated[int,Field(strict=True,ge=0)]] = Field(default_factory=dict)

    _unique_attachments=field_validator('attachments')(unique_references)

    @classmethod
    def for_user(cls,user: AuthenticatedUser,**kwargs):
        # HTTP 装配层只能使用服务端已认证身份，不能信任前端提交的等级映射。
        return cls(platform_permissions=dict(user.platform_permissions),**kwargs)


class CoreSettings(CoreModel):
    max_rounds: int = Field(default=64,strict=True,ge=1)
    max_consecutive_errors: int = Field(default=3,strict=True,ge=1)
    max_compressions: int = Field(default=8,strict=True,ge=0)
    cleanup_timeout_seconds: float = Field(default=5,gt=0,allow_inf_nan=False)
    progress_reminder_interval_seconds: float = Field(default=60,strict=True,ge=0,allow_inf_nan=False,
        description='探索模式中途反馈提醒间隔秒数；0关闭。成功中途输出后重计，未满足的提醒在后续模型请求继续注入。')
    management_reserve_tokens: int = Field(default=1024,strict=True,ge=1)
    file_cache_max_files: int = Field(default=4,strict=True,ge=1)
    page_display_rounds: int = Field(default=5,strict=True,ge=1)
    expert_session_cache_max_sessions: int = Field(default=10,strict=True,ge=1)
    memory_cache_max_queries: int = Field(default=10,strict=True,ge=1)
    memory_page_size: int = Field(default=3,strict=True,ge=1)
    web_search_cache_max_queries: int = Field(default=10,strict=True,ge=1)
    web_search_page_size: int = Field(default=5,strict=True,ge=1)
    web_search_max_concurrent_requests: int = Field(default=2,strict=True,ge=1)
    web_page_cache_max_entries: int = Field(default=10,strict=True,ge=1)
    web_page_chars: int = Field(default=3000,strict=True,ge=1)
    # 数量预算由部署配置决定，不再叠加固定的 20/6 张上限。
    web_image_max_candidates: int = Field(default=6,strict=True,ge=1)
    web_image_max_selected: int = Field(default=3,strict=True,ge=1)
    web_image_max_bytes: int = Field(default=4*1024*1024,strict=True,ge=1)


class AgentResult(CoreModel):
    answer: str
    workspace: WorkspacePayload
    intermediate: list[str]
    rounds: int
    delivery_error_type: str | None = None
