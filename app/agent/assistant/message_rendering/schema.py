"""转换正文的唯一输出契约；原生推理不作为 JSON 字段。"""
from pydantic import BaseModel, ConfigDict, Field
from app.agent.core.json_codec import load_model_json


class MessageRenderingError(ValueError):
    """仅携带本模块校验分支提供的安全原因，不携带原始输出。"""


class RenderedMessage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    content: str = Field(min_length=1, description='仅转换展示格式后的完整钉钉 Markdown 正文，保留原文全部实质内容，不包含推理、转换说明或新增事实。')


def output_schema() -> dict:
    return RenderedMessage.model_json_schema()


def accepted_content(message: dict | None, finish_reason: str | None) -> str:
    if not message or message.get('refusal') or message.get('tool_calls') or finish_reason != 'stop':
        raise MessageRenderingError('转换响应拒绝、截断或包含工具调用，尚未发送；需要完整且未拒绝的结构化正文。')
    raw = message.get('content')
    if not isinstance(raw, str):
        raise MessageRenderingError('转换响应缺少 JSON 正文，尚未发送；需要包含 content 字符串的对象。')
    try:
        result = RenderedMessage.model_validate(load_model_json(raw))
    except (ValueError, TypeError):
        # 不修复 JSON、不回显无效值；本次没有下一轮纠错请求。
        raise MessageRenderingError('转换 JSON 不符合正文 Schema，尚未发送；需要唯一的非空 content 字符串，不接受额外字段、重复键或 JSON 围栏。') from None
    if not result.content.strip():
        raise MessageRenderingError('转换正文为空白，尚未发送；需要完整保留原文内容。')
    return result.content
