"""Agent 内部调用契约；不将工程身份加入模型上下文。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field,JsonValue,field_validator
from app.auth.model import AuthenticatedUser
from app.model.personnel import PlatformCode
from app.model.question_file import AttachmentReference,unique_references
from .workspace import WorkspacePayload, empty_workspace


class CoreModel(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)


class HistoryTurn(CoreModel):
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
    timeout_seconds: float = Field(default=600,gt=0,allow_inf_nan=False)
    cleanup_timeout_seconds: float = Field(default=5,gt=0,allow_inf_nan=False)
    management_reserve_tokens: int = Field(default=1024,strict=True,ge=1)
    soft_limit: float = Field(default=.8,gt=0,lt=1,allow_inf_nan=False)
    file_cache_max_files: int = Field(default=4,strict=True,ge=1)
    page_display_rounds: int = Field(default=5,strict=True,ge=1)
    expert_session_cache_max_sessions: int = Field(default=10,strict=True,ge=1)
    web_search_cache_max_queries: int = Field(default=10,strict=True,ge=1)
    web_search_page_size: int = Field(default=5,strict=True,ge=1)
    web_search_max_concurrent_requests: int = Field(default=2,strict=True,ge=1)
    web_page_cache_max_entries: int = Field(default=10,strict=True,ge=1)
    web_page_chars: int = Field(default=3000,strict=True,ge=1)
    web_image_max_candidates: int = Field(default=6,strict=True,ge=1,le=20)
    web_image_max_selected: int = Field(default=3,strict=True,ge=1,le=6)
    web_image_max_bytes: int = Field(default=4*1024*1024,strict=True,ge=1,le=16*1024*1024)


class AgentResult(CoreModel):
    answer: str
    workspace: WorkspacePayload
    intermediate: list[str]
    rounds: int
    delivery_error_type: str | None = None
