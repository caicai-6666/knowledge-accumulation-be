"""一次结构化生成；请求上下文独立，原始响应先审计后验收。"""
import asyncio
from app.config.mllm import MLLMGenerationSettings, MLLMSettings
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.vllm_tokenizer import count_chat_tokens
from app.model.node_persistence import GenerationPayload
from .prompt import build_messages
from .schema import MessageRenderingError, accepted_content, output_schema


class DingTalkMessageRenderer:
    def __init__(self, settings: MLLMSettings, *, thinking_effort='medium', client=None, counter=None):
        self.settings = settings
        self.generation = MLLMGenerationSettings.model_validate({
            **settings.generation.model_dump(), 'enable_thinking': True,
            'reasoning_effort': thinking_effort,
        })
        self.client = client if client is not None else MLLMClient(settings)
        self._owns_client = client is None
        self.counter = counter
        self._closed = False

    async def render(self, content: str, *, on_generation) -> str:
        if self._closed:
            raise MessageRenderingError('转换器已关闭，尚未发送；需使用有效的转换器实例。')
        if not isinstance(content, str) or not content.strip():
            raise MessageRenderingError('原始答复为空，未请求转换或发送；需提供工具执行后的完整正文。')
        messages = build_messages(content)
        try:
            count = (await self.counter(messages, [], self.generation) if self.counter is not None
                     else await count_chat_tokens(messages, [], self.settings, generation=self.generation))
            if type(count) is not int or count < 0 or count + self.generation.max_completion_tokens > self.settings.context_window_tokens:
                raise MessageRenderingError('转换输入计数无效或超过上下文预算，未生成或发送；需调整模型容量，不能截断原文。')
            # 无工具、无流式回调、无纠错循环；每次 render 最多发起一次生成。
            response = await self.client.create_chat_completion(
                messages=messages, generation=self.generation, json_schema=output_schema())
        except asyncio.CancelledError:
            await on_generation('cancelled', GenerationPayload(
                purpose='assistant_message_rendering', error_type='CancelledError'))
            raise
        except Exception as error:
            await on_generation('failed', GenerationPayload(
                purpose='assistant_message_rendering', error_type=type(error).__name__))
            raise
        # 在解析与业务验收之前可靠保存完整原生推理、JSON、用量及结束原因。
        await on_generation('complete', GenerationPayload(
            purpose='assistant_message_rendering', assistant_message=response.assistant_message,
            raw_response=response.raw_response, raw_chunks=list(response.raw_chunks),
            usage=response.usage, finish_reason=response.finish_reason))
        return accepted_content(response.assistant_message, response.finish_reason)

    async def close(self):
        if not self._closed:
            self._closed = True
            if self._owns_client:
                await self.client.close()
