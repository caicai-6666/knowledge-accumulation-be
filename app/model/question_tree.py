"""问答树存储契约；摘要是独立节点，工作区版本与树内路径绑定。"""

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from app.model.question_file import AttachmentReference,unique_references

TreeID = Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{32}$')]
TreeKind = Literal['assistant', 'exploration']
NodePathType = Literal['main', 'branch']
NodeStatus = Literal['generating','complete','cancelled','superseded','failed']
TERMINAL_NODE_STATUSES = frozenset({'complete','cancelled','superseded','failed'})


def new_tree_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TreeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class QuestionTreeListItem(TreeModel):
    """树选择列表的最小投影，不携带节点、工作区或用户信息。"""

    id: TreeID = Field(description='问答树 ID。')
    name: str = Field(strict=True, min_length=1, max_length=200, description='问答树名称。')


class QuestionTree(TreeModel):
    id: TreeID = Field(default_factory=new_tree_id)
    owner_userid: str = Field(strict=True, min_length=1, max_length=128)
    name: str = Field(strict=True, min_length=1, max_length=200)
    kind: TreeKind = 'exploration'
    main_leaf_id: TreeID | None = None
    revision: int = Field(default=0, strict=True, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator('owner_userid', 'name')
    @classmethod
    def validate_text(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError('树归属和名称不能为空白或包含首尾空白')
        return value


class QuestionNode(TreeModel):
    id: TreeID = Field(default_factory=new_tree_id)
    tree_id: TreeID
    parent_id: TreeID | None = None
    kind: Literal['task', 'summary']
    path_type: NodePathType = 'main'
    status: NodeStatus = 'complete'
    sequence: int = Field(strict=True, ge=1)
    workspace_version_id: TreeID | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    finished_at: AwareDatetime | None = None
    superseded_by_node_id: TreeID | None = None

    @property
    def context_status(self):
        return {'cancelled':'user_manually_stopped','superseded':'user_goal_adjusted'}.get(self.status)

    @property
    def can_interrupt(self):
        return self.kind=='task' and self.status=='generating'

    @property
    def processing_duration_ms(self):
        return None if self.finished_at is None else int((self.finished_at-self.created_at).total_seconds()*1000)

    @model_validator(mode='after')
    def validate_summary(self):
        if self.id == self.parent_id:
            raise ValueError('节点不能以自身为父节点')
        if self.kind == 'summary' and (self.status != 'complete' or self.parent_id is None):
            raise ValueError('摘要必须已完成且存在父节点')
        if (self.status=='superseded')!=(self.superseded_by_node_id is not None):
            raise ValueError('只有被替代节点必须且可以指定接替节点')
        if self.superseded_by_node_id==self.id:
            raise ValueError('节点不能被自身替代')
        if self.finished_at is not None and (self.status=='generating' or self.finished_at<self.created_at):
            raise ValueError('结束时间与节点状态或创建时间不一致')
        return self


class TaskContent(TreeModel):
    kind: Literal['task'] = 'task'
    question: str = Field(strict=True, min_length=1)
    attachments: list[AttachmentReference] = Field(default_factory=list)
    # 保存已接受的 assistant/tool 对与 context_summary 检查点；私有生成独立保存。
    exchanges: list[dict[str, JsonValue]] = Field(default_factory=list)
    answer: str = Field(default='', strict=True)

    _unique_attachments=field_validator('attachments')(unique_references)

    @field_validator('question')
    @classmethod
    def validate_question(cls, value):
        if not value.strip():
            raise ValueError('问题不能为空白')
        return value


class SummaryContent(TreeModel):
    kind: Literal['summary'] = 'summary'
    text: str = Field(strict=True, min_length=1)
    # 记录本次实际压缩的连续区间；是否包含前一份摘要由区间起点确定。
    covered_from_id: TreeID
    covered_to_id: TreeID

    @field_validator('text')
    @classmethod
    def validate_summary_text(cls, value):
        if not value.strip():
            raise ValueError('摘要不能为空白')
        return value


NodeContent = Annotated[TaskContent | SummaryContent, Field(discriminator='kind')]


class NodeWrite(TreeModel):
    """节点元数据与正文一起提交，避免出现没有正文的可见节点。"""
    node: QuestionNode
    content: NodeContent

    @model_validator(mode='after')
    def validate_kind(self):
        if self.node.kind != self.content.kind:
            raise ValueError('节点类型与正文不一致')
        if isinstance(self.content, SummaryContent) and self.content.covered_to_id != self.node.parent_id:
            raise ValueError('摘要覆盖终点必须是摘要的直接父节点')
        return self


class WorkspaceVersion(TreeModel):
    id: TreeID = Field(default_factory=new_tree_id)
    tree_id: TreeID
    content: dict[str, JsonValue]
    created_at: AwareDatetime = Field(default_factory=utc_now)
