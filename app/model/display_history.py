"""公开展示事件；与 SSE 共用负载，绝不包含模型或工具私有轨迹。"""
from typing import Literal
from pydantic import Field, TypeAdapter, model_validator
from app.model.question_tree import TreeID, TreeModel


class DisplayEvent(TreeModel):
    sequence: int | None = Field(default=None, strict=True, ge=1)
    event: Literal['turn.status', 'task.progress', 'message.completed', 'error']
    data: dict

    @model_validator(mode='after')
    def validate_public_payload(self):
        allowed = {
            'turn.status': {'turn_id', 'status', 'can_interrupt', 'context_status', 'supersedes_turn_id',
                'superseded_by_turn_id', 'created_at', 'activated_at', 'finished_at',
                'processing_duration_ms', 'node_id', 'summary_id'},
            'task.progress': {'turn_id', 'type', 'message'},
            'message.completed': {'turn_id', 'message_id', 'message_kind', 'text', 'status'},
            'error': {'turn_id', 'code', 'message', 'retryable'},
        }
        if set(self.data) - allowed[self.event] or 'turn_id' not in self.data:
            raise ValueError('展示事件含未知或私有字段')
        TypeAdapter(TreeID).validate_python(self.data['turn_id'])
        if self.event == 'task.progress':
            message = self.data.get('message')
            if (self.data.get('type') not in ('thinking', 'online-search', 'external-expert', 'local-search', 'a2a', 'boardcast')
                    or not isinstance(message, str) or not message.startswith('正在')
                    or not message[2:].strip() or len(message) > 2000):
                raise ValueError('展示动作不合法')
        return self


class StreamingMessage(TreeModel):
    message_id: TreeID
    message_kind: Literal['intermediate', 'final']
    text: str
    status: Literal['streaming'] = 'streaming'


class StoredDisplayHistory(TreeModel):
    events: tuple[DisplayEvent, ...] = ()
    streaming_messages: tuple[StreamingMessage, ...] = ()
    last_sequence: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode='after')
    def ordered_events(self):
        previous = 0
        for event in self.events:
            if event.sequence is None or event.sequence <= previous or event.sequence > self.last_sequence:
                raise ValueError('展示事件序号必须严格递增且不超过游标')
            previous = event.sequence
        ids = [message.message_id for message in self.streaming_messages]
        completed = {event.data.get('message_id') for event in self.events if event.event == 'message.completed'}
        if len(ids) != len(set(ids)) or set(ids) & completed:
            raise ValueError('流式消息不能重复或与已收束消息冲突')
        return self

    def validate_owner(self, node_id: TreeID):
        if any(event.data['turn_id'] != node_id for event in self.events):
            raise ValueError('展示事件不属于当前任务')
        return self


class TaskDisplayHistory(TreeModel):
    events: tuple[DisplayEvent, ...] = ()
    # 独立展示快照；不得作为权威答案或上下文交互回注模型。
    streaming_messages: tuple[StreamingMessage, ...] = ()
    last_sequence: int | None = Field(default=None, ge=0)
    event_source: Literal['recorded', 'legacy']
