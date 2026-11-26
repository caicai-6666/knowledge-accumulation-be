"""一次结构化生成；请求上下文独立，原始响应先审计后验收。"""
from __future__ import annotations

import asyncio
from typing import Any

from agent_core import VLLMClient
from agent_core.config.mllm import (
    MLLMGenerationSettings as CoreMLLMGenerationSettings,
    MLLMSettings as CoreMLLMSettings,
)
from agent_core.core.model import AgentDependencyError
from agent_core.infrastructure.mllm import MLLMRequestError, MLLMUnavailableError
from agent_core.infrastructure.vllm_tokenizer import count_chat_tokens
from agent_core.model.node_persistence import GenerationPayload

from .prompt import build_messages
from .schema import MessageRenderingError, accepted_content


def _to_core_mllm_settings(settings: Any) -> CoreMLLMSettings:
    if isinstance(settings, CoreMLLMSettings):
        return settings
    if hasattr(settings, 'model_dump'):
        raw = settings.model_dump()
        # 兼容 SecretStr（如 api_key）
        api_key = getattr(settings, 'api_key', None)
        if api_key is not None and hasattr(api_key, 'get_secret_value'):
            raw['api_key'] = api_key.get_secret_value()
        return CoreMLLMSettings.model_validate(raw)
    return CoreMLLMSettings.model_validate(settings)


class DingTalkMessageRenderer:
    """个人助理最终正文钉钉排版转换器；基于 agent-core VLLMClient / ChatProvider。"""

    def __init__(
        self,
        settings: Any,
        *,
        thinking_effort: str = 'medium',
        client: Any = None,
        counter: Any = None,
    ) -> None:
        self.settings = _to_core_mllm_settings(settings)
        # 转换默认启用 medium 推理；独立覆盖本次参数，不改变主助理共用的模型配置。
        # none 不属于底层推理档位，关闭时内部占位为 low，客户端不会发送该档位。
        self.generation = CoreMLLMGenerationSettings.model_validate(
            {
                **self.settings.generation.model_dump(),
                'enable_thinking': thinking_effort != 'none',
                'reasoning_effort': thinking_effort if thinking_effort != 'none' else 'low',
            }
        )
        self.client = client if client is not None else VLLMClient(self.settings)
        self._owns_client = client is None
        self.counter = counter
        self._closed = False

    async def render(self, content: str, *, on_generation) -> str:
        if self._closed:
            raise AgentDependencyError('转换器已关闭，尚未发送；需使用有效的转换器实例。')
        if not isinstance(content, str) or not content.strip():
            raise MessageRenderingError('原始答复为空，未请求转换或发送；需提供工具执行后的完整正文。')
        messages = build_messages(content)

        async def record(status: str, payload: GenerationPayload) -> None:
            try:
                await on_generation(status, payload)
            except Exception as error:
                raise AgentDependencyError('转换审计保存失败，已停止且未发送') from error

        try:
            count = (
                await self.counter(messages, [], self.generation)
                if self.counter is not None
                else await count_chat_tokens(messages, [], self.settings, generation=self.generation)
            )
            if (
                type(count) is not int
                or count < 0
                or count + self.generation.max_completion_tokens > self.settings.context_window_tokens
            ):
                raise MessageRenderingError(
                    '转换输入计数无效或超过上下文预算，未生成或发送；需调整模型容量，不能截断原文。'
                )
            # 无工具、无流式回调、无纠错循环；每次 render 最多发起一次生成。
            # 实测当前服务在 Schema 约束下丢失换行；由提示要求 JSON，响应仍经 Pydantic 严格验收。
            response = await self.client.create_chat_completion(
                messages=messages,
                generation=self.generation,
            )
        except asyncio.CancelledError:
            try:
                await record(
                    'cancelled',
                    GenerationPayload(
                        purpose='assistant_message_rendering',
                        error_type='CancelledError',
                    ),
                )
            except Exception:
                pass  # 已取消时审计尽力保留，不用存储异常替换取消信号。
            raise
        except Exception as error:
            await record(
                'failed',
                GenerationPayload(
                    purpose='assistant_message_rendering',
                    error_type=type(error).__name__,
                ),
            )
            if isinstance(error, (MLLMRequestError, MLLMUnavailableError)):
                raise MessageRenderingError('转换请求未成功，尚未提交或发送；未取得可用转换正文。') from None
            raise
        # 在解析与业务验收之前可靠保存完整原生推理、JSON、用量及结束原因。
        await record(
            'complete',
            GenerationPayload(
                purpose='assistant_message_rendering',
                assistant_message=response.assistant_message,
                raw_response=response.raw_response,
                raw_chunks=list(response.raw_chunks),
                usage=response.usage,
                finish_reason=response.finish_reason,
            ),
        )
        return accepted_content(response.assistant_message, response.finish_reason)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_client:
                await self.client.close()


__all__ = ['DingTalkMessageRenderer']
