"""通用 vLLM Chat Completions；原生推理及原始响应仅交给内部调用方保存。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx
from openai import APIConnectionError, APIResponseValidationError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config.mllm import MLLMGenerationSettings, MLLMSettings
from app.infrastructure.model_concurrency import get_model_request_limiter


class MLLMRequestError(RuntimeError):
    """请求或响应协议不合法；不携带用户正文、服务地址或密钥。"""


class MLLMUnavailableError(RuntimeError):
    """连接、限流或服务故障；不自动重放生成请求。"""


class MLLMProviderNotReadyError(MLLMUnavailableError):
    """模型或接口不存在（404）。"""


@dataclass(frozen=True)
class MLLMToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class MLLMCompletion:
    response_id: str | None
    model: str | None
    assistant_message: dict[str, Any]
    tool_calls: tuple[MLLMToolCall, ...]
    finish_reason: str
    usage: dict[str, Any]
    raw_response: dict[str, Any]
    raw_chunks: tuple[dict[str, Any], ...] = ()

    @property
    def content(self) -> str | None:
        return self.assistant_message.get('content')


def build_api_base_url(base_url) -> str:
    base = str(base_url).rstrip('/')
    return (base if base.endswith('/v1') else base+'/v1')+'/'


def _completion(raw, chunks=()) -> MLLMCompletion:
    choices = raw.get('choices')
    if not isinstance(choices,list) or len(choices)!=1 or not isinstance(choices[0],dict):
        raise MLLMRequestError('模型必须返回唯一的生成候选')
    choice=choices[0]
    message=choice.get('message')
    if not isinstance(message,dict) or message.get('role')!='assistant':
        raise MLLMRequestError('模型响应缺少合法的助手消息')
    if not isinstance(choice.get('finish_reason'),str) or not choice['finish_reason']:
        raise MLLMRequestError('模型响应缺少结束原因')
    for field in ('content','reasoning','reasoning_content','refusal'):
        if message.get(field) is not None and not isinstance(message[field],str):
            raise MLLMRequestError('模型消息字段类型不合法')
    calls=[]
    seen=set()
    tool_calls=message.get('tool_calls')
    if tool_calls is not None and not isinstance(tool_calls,list):
        raise MLLMRequestError('模型工具列表类型不合法')
    for call in tool_calls or []:
        function=call.get('function') if isinstance(call,dict) else None
        if (not isinstance(function,dict) or call.get('type')!='function'
                or not isinstance(call.get('id'),str) or not call['id'] or call['id'] in seen
                or not isinstance(function.get('name'),str) or not function['name']
                or not isinstance(function.get('arguments'),str)):
            raise MLLMRequestError('模型工具调用结构不合法')
        seen.add(call['id'])
        # 参数原文完整保留；JSON 与业务参数验收留给工具层，不自动执行工具。
        calls.append(MLLMToolCall(call['id'],function['name'],function['arguments']))
    usage=raw.get('usage')
    if usage is None:
        usage={}
    if not isinstance(usage,dict):
        raise MLLMRequestError('模型用量字段不合法')
    for field in ('prompt_tokens','completion_tokens','total_tokens'):
        if field in usage and usage[field] is not None and (type(usage[field]) is not int or usage[field]<0):
            raise MLLMRequestError('模型用量字段不合法')
    return MLLMCompletion(raw.get('id'),raw.get('model'),deepcopy(message),tuple(calls),
        choice['finish_reason'],deepcopy(usage),deepcopy(raw),tuple(deepcopy(chunks)))


class MLLMClient:
    def __init__(self, settings: MLLMSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings=settings
        self._closed=False
        self._http=httpx.AsyncClient(timeout=settings.timeout_seconds,trust_env=False,
            follow_redirects=False,transport=transport)
        self._client=AsyncOpenAI(api_key=settings.api_key.get_secret_value() or 'vllm-local',
            base_url=build_api_base_url(settings.base_url),http_client=self._http,
            timeout=settings.timeout_seconds,max_retries=0)

    async def __aenter__(self):
        return self

    async def __aexit__(self,*args):
        await self.close()

    async def close(self):
        if not self._closed:
            self._closed=True
            await self._client.close()

    def _template(self, generation, supplied):
        template=deepcopy(supplied or {})
        # 思考档位由本次受验收的配置决定；生成与分词使用相同映射。
        template.pop('reasoning_effort',None)
        template.update(self.settings.thinking_template_kwargs(generation=generation))
        return template

    async def create_chat_completion(self, *, messages: list[dict], tools: list[dict] | None = None,
            tool_choice: str | dict = 'auto', generation: MLLMGenerationSettings | None = None,
            stream: bool = False, on_delta: Callable[[dict],Awaitable[None]] | None = None,
            on_tool_delta: Callable[[dict],Awaitable[None]] | None = None,
            chat_template_kwargs: dict | None = None,
            json_schema: dict | None = None,
            on_raw_chunk: Callable[[dict],Awaitable[None]] | None = None) -> MLLMCompletion:
        if self._closed:
            raise MLLMRequestError('模型客户端已关闭')
        if not messages or not isinstance(messages,list):
            raise ValueError('模型消息不能为空')
        current=generation or self.settings.generation
        if not isinstance(current,MLLMGenerationSettings):
            raise ValueError('生成配置类型不合法')
        current=MLLMGenerationSettings.model_validate(current.model_dump())
        if current.max_completion_tokens>=self.settings.context_window_tokens:
            raise ValueError('最大生成长度必须小于上下文窗口')
        streaming=stream or on_delta is not None or on_tool_delta is not None or on_raw_chunk is not None
        request=dict(model=self.settings.model,messages=deepcopy(messages),
            max_completion_tokens=current.max_completion_tokens,temperature=current.temperature,
            top_p=current.top_p,presence_penalty=current.presence_penalty,seed=current.seed,stream=streaming,
            extra_body={'top_k':current.top_k,'repetition_penalty':current.repetition_penalty,
                'chat_template_kwargs':self._template(current,chat_template_kwargs)})
        if tools:
            request.update(tools=deepcopy(tools),tool_choice=deepcopy(tool_choice),parallel_tool_calls=False)
        if current.min_p is not None:
            request['extra_body']['min_p'] = current.min_p
        if json_schema is not None:
            if tools or not isinstance(json_schema, dict) or not json_schema:
                raise ValueError('JSON约束生成必须提供Schema且不能同时启用工具')
            request['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'structured_output', 'strict': True, 'schema': deepcopy(json_schema)}}
        if streaming:
            request['stream_options']={'include_usage':True}
        try:
            # 整体时限涵盖配额等待及流式消费；取消直接传播，并关闭响应流。
            async with asyncio.timeout(self.settings.timeout_seconds), get_model_request_limiter('mllm',self.settings.max_concurrent_requests):
                response=await self._client.chat.completions.create(**request)
                if streaming:
                    return await self._consume(response,on_delta,on_raw_chunk,on_tool_delta)
                if not callable(getattr(response,'model_dump',None)):
                    raise MLLMRequestError('模型响应不是合法的 JSON 对象')
                return _completion(response.model_dump(mode='json',exclude_none=False))
        except APIStatusError as error:
            if error.status_code==404:
                raise MLLMProviderNotReadyError('模型服务或模型尚未准备好') from None
            if error.status_code>=500 or error.status_code in (408,409,429):
                raise MLLMUnavailableError('模型服务暂不可用') from None
            raise MLLMRequestError('模型接口拒绝了请求') from None
        except (APIConnectionError,APITimeoutError,TimeoutError):
            raise MLLMUnavailableError('模型连接失败或请求超时') from None
        except (APIResponseValidationError,json.JSONDecodeError):
            raise MLLMRequestError('模型响应格式不合法') from None

    async def create_tool_chat_completion(self, *, tools: list[dict], **kwargs):
        if not tools:
            raise ValueError('工具请求至少需要一个工具')
        return await self.create_chat_completion(tools=tools,**kwargs)

    async def _consume(self, response, on_delta, on_raw_chunk=None, on_tool_delta=None):
        chunks=[]
        message={'role':'assistant','content':None}
        calls={}
        finish=None
        identity={}
        usage={}
        try:
            async for chunk in response:
                raw=chunk.model_dump(mode='json',exclude_none=False)
                chunks.append(raw)
                if on_raw_chunk is not None:
                    # 审计回调先于解析；断流或非法片段也能保留已接收的原始证据。
                    await on_raw_chunk(deepcopy(raw))
                for field in ('id','model'):
                    if raw.get(field) is not None:
                        if field in identity and identity[field]!=raw[field]:
                            raise MLLMRequestError('流式响应身份不一致')
                        identity[field]=raw[field]
                if raw.get('usage') is not None:
                    usage=raw['usage']
                choices=raw.get('choices')
                if choices==[]:
                    continue  # include_usage 允许末尾用量块没有候选。
                if finish is not None:
                    raise MLLMRequestError('流式响应结束后仍返回了内容')
                if (not isinstance(choices,list) or len(choices)!=1 or not isinstance(choices[0],dict)
                        or choices[0].get('index')!=0):
                    raise MLLMRequestError('流式响应候选不合法')
                choice=choices[0]
                delta=choice.get('delta')
                if not isinstance(delta,dict) or delta.get('role') not in (None,'assistant'):
                    raise MLLMRequestError('流式助手消息不合法')
                for field in ('content','reasoning','reasoning_content','refusal'):
                    value=delta.get(field)
                    if value is not None:
                        if not isinstance(value,str):
                            raise MLLMRequestError('流式文本字段不合法')
                        message[field]=(message.get(field) or '')+value
                tool_calls=delta.get('tool_calls')
                if tool_calls is not None and not isinstance(tool_calls,list):
                    raise MLLMRequestError('流式工具列表类型不合法')
                for call in tool_calls or []:
                    if not isinstance(call,dict):
                        raise MLLMRequestError('流式工具调用类型不合法')
                    index=call.get('index')
                    if type(index) is not int or index<0:
                        raise MLLMRequestError('流式工具索引不合法')
                    target=calls.setdefault(index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                    if call.get('type') not in (None,'function'):
                        raise MLLMRequestError('流式工具类型不合法')
                    if call.get('id') is not None:
                        if not isinstance(call['id'],str):
                            raise MLLMRequestError('流式工具标识不合法')
                        target['id']+=call['id']
                    function=call.get('function') or {}
                    if not isinstance(function,dict):
                        raise MLLMRequestError('流式工具参数类型不合法')
                    for field in ('name','arguments'):
                        value=function.get(field)
                        if value is not None:
                            if not isinstance(value,str):
                                raise MLLMRequestError('流式工具参数不合法')
                            target['function'][field]+=value
                if choice.get('finish_reason') is not None:
                    if finish is not None:
                        raise MLLMRequestError('流式响应重复结束')
                    finish=choice['finish_reason']
                if on_tool_delta is not None:
                    # 独立的累计工具快照供正文解析；原有 on_delta 仍保持原始分片契约。
                    await on_tool_delta(deepcopy({'content': message.get('content'),
                        'refusal': message.get('refusal'),
                        'tool_calls': [calls[index] for index in sorted(calls)]}))
                if on_delta is not None:
                    await on_delta(deepcopy(delta))
        finally:
            await response.close()
        if finish is None:
            raise MLLMUnavailableError('模型流式响应未完整结束')
        if calls:
            if sorted(calls)!=list(range(len(calls))):
                raise MLLMRequestError('流式工具序号不连续')
            message['tool_calls']=[calls[index] for index in sorted(calls)]
        raw={**identity,'choices':[{'index':0,'finish_reason':finish,'message':message}],'usage':usage}
        return _completion(raw,chunks)
