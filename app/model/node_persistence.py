"""可恢复节点的公开消息与私有生成记录；推理不采用窗口或长度截断。"""

from dataclasses import dataclass
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from app.model.question_tree import QuestionNode, QuestionTree, TaskContent, TreeID, TreeModel, WorkspaceVersion, new_tree_id, utc_now


class NodeMessage(TreeModel):
    id: TreeID = Field(default_factory=new_tree_id)
    node_id: TreeID
    kind: Literal['question','intermediate','final']
    status: Literal['completed','interrupted'] = 'completed'
    text: str = Field(strict=True, min_length=1)
    sequence: int | None = Field(default=None, strict=True, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator('text')
    @classmethod
    def validate_text(cls,value):
        if not value.strip():
            raise ValueError('消息正文不能为空白')
        return value

    @model_validator(mode='after')
    def validate_question(self):
        if self.kind=='question' and self.status!='completed':
            raise ValueError('用户问题不能标为中断输出')
        return self


class GenerationPayload(TreeModel):
    # 工程用途只存在私有审计中，不能拼入模型消息。
    purpose: Literal['main','history_summary','context_compaction','workspace_compaction','external_expert','web_page_refinement'] = 'main'
    assistant_message: dict[str, JsonValue] | None = None
    raw_response: dict[str, JsonValue] | None = None
    raw_chunks: list[dict[str, JsonValue]] = Field(default_factory=list)
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    finish_reason: str | None = Field(default=None, strict=True)
    error_type: str | None = Field(default=None, strict=True, pattern=r'^[A-Za-z][A-Za-z0-9_]*$')


class NodeGeneration(TreeModel):
    id: TreeID = Field(default_factory=new_tree_id)
    node_id: TreeID
    status: Literal['complete','failed','cancelled']
    payload: GenerationPayload
    sequence: int | None = Field(default=None, strict=True, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @classmethod
    def from_completion(cls,node_id,completion,*,generation_id=None):
        values={'node_id':node_id,'status':'complete','payload':GenerationPayload(
            assistant_message=completion.assistant_message,raw_response=completion.raw_response,
            raw_chunks=list(completion.raw_chunks),usage=completion.usage,finish_reason=completion.finish_reason)}
        if generation_id is not None:
            values['id']=generation_id
        return cls.model_validate(values)

    @model_validator(mode='after')
    def validate_completed_generation(self):
        if self.status=='complete' and self.payload.assistant_message is None:
            raise ValueError('完成的生成必须保存原始助手消息')
        return self


@dataclass(frozen=True)
class RestoredNode:
    tree: QuestionTree
    node: QuestionNode
    content: TaskContent
    workspace: WorkspaceVersion
    messages: tuple[NodeMessage, ...]
    generations: tuple[NodeGeneration, ...]

    @property
    def events(self) -> tuple[NodeMessage | NodeGeneration, ...]:
        return tuple(sorted((*self.messages,*self.generations),key=lambda event:event.sequence))
