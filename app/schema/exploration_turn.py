"""探索轮次的公开输入和展示快照；不暴露模型轨迹与用户凭据。"""
from datetime import datetime
from typing import Literal
from pydantic import Field, model_validator
from app.model.question_tree import TreeID, TreeModel


class TurnInput(TreeModel):
    selected_node_id: TreeID | None = None
    mode: Literal['main', 'branch'] = Field(description='创建模式：main 为推进，生成主干节点；branch 为探索，生成分支节点。非空树仅当前主干末尾允许推进。')
    question: str = Field(strict=True, min_length=1, max_length=20000, pattern=r'\S')
    thinking_effort: Literal['low', 'medium', 'xhigh']
    supersedes_turn_id: TreeID | None = None

    @model_validator(mode='after')
    def validate_replacement(self):
        if self.supersedes_turn_id is not None and self.selected_node_id not in (None, self.supersedes_turn_id):
            raise ValueError('改变方向不能同时选择其他节点')
        return self


class TurnCreated(TreeModel):
    tree_id: TreeID
    turn_id: TreeID
    status: Literal['pending_activation'] = 'pending_activation'
    can_interrupt: bool = True
    activation_expires_at: datetime
    supersedes_turn_id: TreeID | None = None


class TurnMessage(TreeModel):
    message_id: TreeID
    message_kind: Literal['intermediate', 'final']
    text: str
    status: Literal['completed'] = 'completed'


class TurnSnapshot(TreeModel):
    tree_id: TreeID
    turn_id: TreeID
    node_id: TreeID | None = None
    summary_id: TreeID | None = None
    supersedes_turn_id: TreeID | None = None
    superseded_by_turn_id: TreeID | None = None
    status: Literal['pending_activation', 'processing', 'completed', 'cancelled', 'superseded', 'failed', 'expired']
    can_interrupt: bool
    context_status: Literal['user_manually_stopped', 'user_goal_adjusted'] | None = None
    activation_expires_at: datetime
    activated_at: datetime | None = None
    finished_at: datetime | None = None
    processing_duration_ms: int | None = None
    last_sequence: int
    earliest_sequence: int
    messages: tuple[TurnMessage, ...] = ()
    progress: dict | None = Field(default=None, description='最近一次工具动作提示；终态保留用于展示，不表示仍在执行。')
    error: dict | None = None
