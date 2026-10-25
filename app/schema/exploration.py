"""探索问答树 HTTP 请求契约。"""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from app.model.question_file import AttachmentReference
from app.model.display_history import TaskDisplayHistory
from app.model.question_tree import NodePathType, NodeStatus, QuestionTreeListItem, SummaryContent, TreeID, TreeModel


class ExplorationTreeListItem(QuestionTreeListItem):
    has_running_task: bool = Field(strict=True, description='当前树是否有正在执行的任务；待激活及已结束任务为 false。')


class CreateTreeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)] = Field(
        default_factory=lambda: datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'),
        description='探索树名称，去除首尾空白后为 1–200 个字符；省略时按本次请求的北京时间生成 YYYY-MM-DD HH:mm:ss。')


class RenameTreeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)] = Field(
        description='新的问答树名称，去除首尾空白后须为 1–200 个字符。')


class TaskDisplayContent(TreeModel):
    """只展示用户内容；工具交互和原生推理不进入前端历史。"""

    kind: Literal['task'] = 'task'
    question: str
    attachments: tuple[AttachmentReference, ...] = ()
    answer: str


class MessageDisplay(TreeModel):
    id: TreeID
    kind: Literal['question', 'intermediate', 'final']
    status: Literal['completed', 'interrupted']
    text: str
    sequence: int = Field(ge=1)
    created_at: AwareDatetime


class ExplorationNodeResponse(TreeModel):
    id: TreeID
    parent_id: TreeID | None
    children_ids: tuple[TreeID, ...]
    kind: Literal['task', 'summary']
    path_type: NodePathType
    status: NodeStatus
    sequence: int = Field(ge=1)
    created_at: AwareDatetime
    finished_at: AwareDatetime | None
    superseded_by_node_id: TreeID | None
    context_status: Literal['user_manually_stopped', 'user_goal_adjusted'] | None
    can_interrupt: bool
    processing_duration_ms: int | None
    content: Annotated[TaskDisplayContent | SummaryContent, Field(discriminator='kind')]
    messages: tuple[MessageDisplay, ...]
    display: TaskDisplayHistory | None = Field(default=None, description='任务的精简展示历史；摘要节点为 null。recorded 为真实事件，legacy 只恢复已有公开输出。')


class ExplorationTreeResponse(TreeModel):
    id: TreeID
    name: str
    kind: Literal['exploration'] = 'exploration'
    root_id: TreeID | None
    main_leaf_id: TreeID | None
    revision: int = Field(ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    nodes: tuple[ExplorationNodeResponse, ...] = Field(description='按 sequence 排序的完整节点数组，含所有主干、分支与摘要，不分页或按最近摘要裁剪。')
