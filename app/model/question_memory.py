"""节点记忆只保存来源引用和向量，不复制原文或逐条保存模型配置。"""

from math import isfinite
import struct
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.model.question_tree import TreeID, TreeModel, new_tree_id, utc_now


def normalize_embedding(value) -> tuple[float, ...]:
    """统一为有限的 float32，保证写入与幂等比较使用同一数值精度。"""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError('向量必须是非空数值序列')
    result = []
    for component in value:
        if type(component) not in (int, float):
            raise ValueError('向量分量必须是数值')
        try:
            number = struct.unpack('<f', struct.pack('<f', component))[0]
        except (OverflowError, struct.error):
            raise ValueError('向量分量超出 float32 范围') from None
        if not isfinite(number):
            raise ValueError('向量分量必须是有限数值')
        result.append(number)
    return tuple(result)


def encode_embedding(value: tuple[float, ...]) -> bytes:
    return struct.pack(f'<{len(value)}f', *value)


def decode_embedding(value: bytes | None) -> tuple[float, ...] | None:
    if value is None:
        return None
    if not value or len(value) % 4:
        raise ValueError('记忆向量二进制格式不合法')
    return struct.unpack(f'<{len(value) // 4}f', value)


class QuestionNodeMemory(TreeModel):
    id: TreeID = Field(default_factory=new_tree_id)
    node_id: TreeID
    source_id: str = Field(strict=True, min_length=1, max_length=128)
    kind: Literal['question', 'intermediate', 'final']
    embedding: tuple[float, ...] | None = None
    embedding_status: Literal['pending', 'success', 'failed'] = 'pending'
    created_at: AwareDatetime = Field(default_factory=utc_now)
    embedded_at: AwareDatetime | None = None

    @field_validator('source_id')
    @classmethod
    def validate_source(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError('来源标识不能为空白或包含首尾空白')
        return value

    @field_validator('embedding', mode='before')
    @classmethod
    def validate_vector(cls, value):
        return normalize_embedding(value) if value is not None else None

    @model_validator(mode='after')
    def validate_status(self):
        ready = self.embedding_status == 'success'
        if ready != (self.embedding is not None) or ready != (self.embedded_at is not None):
            raise ValueError('向量、处理状态与完成时间不一致')
        return self


class SearchableMemory(TreeModel):
    """检索快照携带原文副本，不向权威记忆表增加 text 字段。"""
    memory: QuestionNodeMemory
    text: str = Field(strict=True)
