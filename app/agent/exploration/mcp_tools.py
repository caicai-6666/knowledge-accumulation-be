"""探索对话 MCP 工具适配器：将 MCP 工具封装为 agent-core RegisteredTool。

职责边界：
- 动态将 MCP JSON Schema 转为严格 Pydantic BaseModel，完整保留各级字段 description。
- 将 MCP `meta.progress` 映射为 `ToolProgress`。
- 为 `view_web_search_results` 与 `open_web_page` 提供可折叠页面与按需解析（含图文拆分）。
- 按错误码精确分层为 `correctable` / `failed` / `dependency`，并经末尾 `FeedbackOnlyHook` 渲染清晰中文正文。
"""
from __future__ import annotations

from collections.abc import Sequence
from functools import partial
import hashlib
import hmac
import secrets
from textwrap import dedent
from typing import Annotated, Any, Literal
from uuid import uuid4

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field, create_model

from agent_core.core.tool.cache import LruCache
from agent_core.core.tool.feedback import with_feedback
from agent_core.core.tool.hooks import FeedbackOnlyHook
from agent_core.core.tool.progress import DEFAULT_TOOL_PROGRESS, ToolProgress
from agent_core.core.tool.registry import RegisteredTool, check_descriptions
from agent_core.core.tool.result import (
    FoldableToolContent,
    OrdinaryToolContent,
    ToolExecution,
    ToolExecutionResult,
    ToolPageReference,
    ToolPageUnavailable,
)

from app.infrastructure.mcp_client import MCPClientManager, MCPUnavailableError

EXPLORATION_MCP_TOOL_NAMES: tuple[str, ...] = (
    'get_current_time',
    'calculate_date',
    'search_web',
    'view_web_search_results',
    'open_web_page',
    'ask_external_expert',
    'continue_external_expert',
)

_ALLOWED_PROGRESS_TYPES = frozenset({
    'thinking',
    'online-search',
    'external-expert',
    'local-search',
    'a2a',
    'boardcast',
})

_TOOL_TITLES: dict[str, str] = {
    'get_current_time': '当前时间查询',
    'calculate_date': '日期时间计算',
    'search_web': '网页搜索',
    'view_web_search_results': '网页搜索结果查看',
    'open_web_page': '网页读取',
    'ask_external_expert': '外部专家咨询',
    'continue_external_expert': '外部专家续聊',
}

# 按工具精确维护可纠正错误码；其余已确认执行失败归类为 failed，传输/服务不可用归类为 dependency。
_CORRECTABLE_ERROR_CODES: dict[str, frozenset[str]] = {
    'search_web': frozenset({'invalid_arguments'}),
    'view_web_search_results': frozenset({
        'invalid_arguments',
        'invalid_page',
        'end_of_results',
        'result_expired',
    }),
    'open_web_page': frozenset({
        'invalid_arguments',
        'invalid_page',
        'end_of_results',
        'focus_required',
        'source_expired',
        'source_not_openable',
        'session_expired',
    }),
    'ask_external_expert': frozenset({
        'session_expired',
        'session_busy',
        'session_limit',
        'incomplete_response',
    }),
    'continue_external_expert': frozenset({
        'session_expired',
        'session_busy',
        'session_limit',
        'incomplete_response',
    }),
}

_FAILED_ERROR_CODES: dict[str, frozenset[str]] = {
    'search_web': frozenset({'search_failed'}),
    'open_web_page': frozenset({
        'web_page_failed',
        'response_too_large',
        'http_timeout',
        'http_status_error',
        'too_many_redirects',
        'http_request_failed',
        'extraction_failed',
        'refinement_request_failed',
        'invalid_model_output',
        'invalid_processing_result',
    }),
    'ask_external_expert': frozenset({
        'expert_request_failed',
        'pool_busy',
        'pool_closed',
        'expert_unavailable',
    }),
    'continue_external_expert': frozenset({
        'expert_request_failed',
        'pool_busy',
        'pool_closed',
        'expert_unavailable',
    }),
}


def _resolve_json_ref(ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith('#/'):
        raise ValueError(f'不支持外部 JSON Schema 引用：{ref}')
    target: Any = root_schema
    for part in ref[2:].split('/'):
        if not isinstance(target, dict) or part not in target:
            raise ValueError(f'无法解析 JSON Schema 引用：{ref}')
        target = target[part]
    if not isinstance(target, dict):
        raise ValueError(f'JSON Schema 引用目标不是对象：{ref}')
    return target


def _extract_field_constraints(schema: dict[str, Any]) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    if isinstance(schema.get('minLength'), int):
        constraints['min_length'] = schema['minLength']
    if isinstance(schema.get('maxLength'), int):
        constraints['max_length'] = schema['maxLength']
    if isinstance(schema.get('pattern'), str):
        constraints['pattern'] = schema['pattern']
    if isinstance(schema.get('minimum'), (int, float)):
        constraints['ge'] = schema['minimum']
    if isinstance(schema.get('maximum'), (int, float)):
        constraints['le'] = schema['maximum']
    if isinstance(schema.get('exclusiveMinimum'), (int, float)):
        constraints['gt'] = schema['exclusiveMinimum']
    if isinstance(schema.get('exclusiveMaximum'), (int, float)):
        constraints['lt'] = schema['exclusiveMaximum']
    if isinstance(schema.get('minItems'), int):
        constraints['min_length'] = schema['minItems']
    if isinstance(schema.get('maxItems'), int):
        constraints['max_length'] = schema['maxItems']
    return constraints


def _schema_to_python_type(
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any],
    model_prefix: str,
) -> tuple[Any, dict[str, Any]]:
    if '$ref' in schema:
        resolved = _resolve_json_ref(schema['$ref'], root_schema)
        merged = {**resolved, **{k: v for k, v in schema.items() if k != '$ref'}}
        return _schema_to_python_type(merged, root_schema=root_schema, model_prefix=model_prefix)

    # 处理 Pydantic v2 常见的 anyOf / oneOf（如 T | None 或多分支联合类型）
    for union_key in ('anyOf', 'oneOf'):
        if isinstance(schema.get(union_key), list):
            branches = schema[union_key]
            non_null = [branch for branch in branches if branch.get('type') != 'null']
            allows_null = len(non_null) < len(branches)
            if not non_null:
                return type(None), {}
            if len(non_null) == 1:
                inner_type, inner_constraints = _schema_to_python_type(
                    non_null[0],
                    root_schema=root_schema,
                    model_prefix=model_prefix,
                )
                merged_constraints = {**inner_constraints, **_extract_field_constraints(schema)}
                if allows_null:
                    if merged_constraints:
                        return Annotated[inner_type, Field(**merged_constraints)] | None, {}
                    return inner_type | None, {}
                return inner_type, merged_constraints

            resolved_types = []
            for idx, branch in enumerate(non_null):
                b_type, b_constraints = _schema_to_python_type(
                    branch,
                    root_schema=root_schema,
                    model_prefix=f'{model_prefix}Variant{idx}',
                )
                if b_constraints:
                    b_type = Annotated[b_type, Field(**b_constraints)]
                resolved_types.append(b_type)
            union_type = resolved_types[0]
            for item in resolved_types[1:]:
                union_type = union_type | item
            if allows_null:
                union_type = union_type | None
            return union_type, _extract_field_constraints(schema)

    if isinstance(schema.get('enum'), list) and schema['enum']:
        return Literal[tuple(schema['enum'])], _extract_field_constraints(schema)

    schema_type = schema.get('type')
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != 'null']
        allows_null = 'null' in schema_type
        if len(non_null_types) == 1:
            sub = {**schema, 'type': non_null_types[0]}
            inner_type, constraints = _schema_to_python_type(
                sub,
                root_schema=root_schema,
                model_prefix=model_prefix,
            )
            if allows_null:
                if constraints:
                    return Annotated[inner_type, Field(**constraints)] | None, {}
                return inner_type | None, {}
            return inner_type, constraints

    if schema_type == 'string':
        return str, _extract_field_constraints(schema)
    if schema_type == 'integer':
        return int, _extract_field_constraints(schema)
    if schema_type == 'number':
        return float, _extract_field_constraints(schema)
    if schema_type == 'boolean':
        return bool, {}
    if schema_type == 'array':
        items_schema = schema.get('items')
        if isinstance(items_schema, dict):
            item_type, item_constraints = _schema_to_python_type(
                items_schema,
                root_schema=root_schema,
                model_prefix=f'{model_prefix}Item',
            )
            if item_constraints:
                item_type = Annotated[item_type, Field(**item_constraints)]
            return list[item_type], _extract_field_constraints(schema)
        return list[Any], _extract_field_constraints(schema)
    if schema_type == 'object' or 'properties' in schema:
        properties = schema.get('properties')
        if isinstance(properties, dict):
            nested_model = _build_object_model(
                schema,
                root_schema=root_schema,
                model_name=model_prefix,
            )
            return nested_model, {}
        return dict[str, Any], {}

    return Any, _extract_field_constraints(schema)


def _build_object_model(
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any],
    model_name: str,
) -> type[BaseModel]:
    properties = schema.get('properties') or {}
    if not isinstance(properties, dict):
        raise ValueError(f'模型 {model_name} 的 properties 必须为对象')
    required_set = set(schema.get('required') or ())

    fields: dict[str, tuple[Any, Any]] = {}
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            raise ValueError(f'模型 {model_name} 参数 {prop_name} 定义不合法')
        description = prop_schema.get('description')
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f'模型 {model_name} 参数 {prop_name} 缺少非空 description')

        camel = ''.join(part.capitalize() for part in prop_name.split('_') if part)
        py_type, constraints = _schema_to_python_type(
            prop_schema,
            root_schema=root_schema,
            model_prefix=f'{model_name}{camel}',
        )

        is_required = prop_name in required_set
        has_default = 'default' in prop_schema
        field_kwargs: dict[str, Any] = {'description': description.strip(), **constraints}
        if is_required and not has_default:
            fields[prop_name] = (py_type, Field(..., **field_kwargs))
        else:
            default_value = prop_schema.get('default', None)
            if default_value is None and py_type is not Any:
                # 确保可选无默认值字段接受 None
                try:
                    py_type = py_type | None
                except TypeError:
                    pass
            fields[prop_name] = (py_type, Field(default=default_value, **field_kwargs))

    # 无参对象（如 get_current_time）放宽为 ignore，避免真实模型补填占位键触发连续拒收，
    # 实际调用 MCP 时 model_dump() 仍固定输出 {}；有参对象继续严格 forbid 未定义字段。
    if not properties:
        extra_policy = 'ignore'
    else:
        extra_policy = 'forbid' if schema.get('additionalProperties') is False or 'additionalProperties' not in schema else 'allow'
    return create_model(
        model_name,
        __config__=ConfigDict(extra=extra_policy, strict=True, frozen=True),
        **fields,
    )


def json_schema_to_pydantic_model(tool_name: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    """将 MCP 工具的 JSON Schema 动态转换为严格 Pydantic BaseModel 并校验全部字段说明。"""
    if not isinstance(input_schema, dict) or input_schema.get('type') != 'object':
        raise ValueError(f'MCP 工具 {tool_name} 的 inputSchema 顶层必须为 object')
    camel = ''.join(part.capitalize() for part in tool_name.replace('-', '_').split('_') if part)
    model = _build_object_model(
        input_schema,
        root_schema=input_schema,
        model_name=f'MCP{camel}Arguments',
    )
    # 立即按 ToolRegistry 同一规则验收最终生成的 JSON Schema
    check_descriptions(model.model_json_schema())
    return model


def extract_tool_progress(raw_tool: mcp_types.Tool) -> ToolProgress:
    """将 MCP `meta.progress` 映射为 `agent_core.core.tool.progress.ToolProgress`。"""
    meta = raw_tool.meta if isinstance(raw_tool.meta, dict) else {}
    raw_progress = meta.get('progress')
    if not isinstance(raw_progress, dict):
        return DEFAULT_TOOL_PROGRESS
    progress_type = raw_progress.get('type')
    message = raw_progress.get('message')
    if (
        not isinstance(message, str)
        or not message.startswith('正在')
        or not message[2:].strip()
        or len(message) > 2000
    ):
        return DEFAULT_TOOL_PROGRESS
    resolved_type = progress_type if progress_type in _ALLOWED_PROGRESS_TYPES else 'thinking'
    return ToolProgress(type=resolved_type, message=message)


def _extract_text_blocks(content: Sequence[Any] | None) -> list[str]:
    texts: list[str] = []
    for block in content or ():
        if isinstance(block, mcp_types.TextContent) and isinstance(block.text, str) and block.text.strip():
            texts.append(block.text.strip())
    return texts


def _extract_image_blocks(content: Sequence[Any] | None) -> list[mcp_types.ImageContent]:
    return [
        block
        for block in (content or ())
        if isinstance(block, mcp_types.ImageContent) and isinstance(block.data, str) and block.data.strip()
    ]


def classify_mcp_error(
    tool_name: str,
    structured: dict[str, Any] | None,
) -> Literal['correctable', 'failed', 'dependency']:
    """按工具及 MCP `structuredContent` 错误码映射为 agent-core 失败分层。"""
    # MCP SDK 参数 Schema 校验拒绝时 structuredContent 为空，统一归类为可修正错误。
    if not isinstance(structured, dict) or not structured:
        return 'correctable'
    if tool_name == 'calculate_date':
        return 'correctable'
    error_code = str(structured.get('error_code') or '').strip()
    if error_code in _CORRECTABLE_ERROR_CODES.get(tool_name, frozenset()):
        return 'correctable'
    if error_code in _FAILED_ERROR_CODES.get(tool_name, frozenset()):
        return 'failed'
    if not error_code:
        return 'correctable'
    return 'failed'


def _default_required_action(
    tool_name: str,
    failure_mode: Literal['correctable', 'failed', 'dependency'],
) -> str:
    if failure_mode == 'dependency':
        return '外部工具服务当前不可用，请暂缓重放并向用户说明服务限制。'
    if failure_mode == 'correctable':
        if tool_name in {'view_web_search_results', 'open_web_page'}:
            return '请核对来源或结果 ID、页码及必填参数后重试；若结果已过期，请重新搜索。'
        if tool_name in {'ask_external_expert', 'continue_external_expert'}:
            return '请核对专家会话 ID 与提问内容后重试；若会话已过期或达上限，请重新发起咨询。'
        return '请检查并修正工具参数后重新调用。'
    if tool_name == 'search_web':
        return '可尝试调整检索词重试，或直接基于已有资料作答并说明联网检索受限。'
    if tool_name == 'open_web_page':
        return '请改用其他搜索来源或调整检索策略，不能根据失败推断网页不存在。'
    if tool_name in {'ask_external_expert', 'continue_external_expert'}:
        return '外部专家本次未能返回有效答复，请改用其他检索手段或向用户说明限制后继续作答。'
    return '请结合已有信息调整方案，或向用户诚实说明本次查询未完成。'


def _build_mcp_error_result(
    tool_name: str,
    call_result: mcp_types.CallToolResult,
) -> ToolExecutionResult:
    structured = call_result.structured_content if isinstance(call_result.structured_content, dict) else None
    failure_mode = classify_mcp_error(tool_name, structured)
    text_blocks = _extract_text_blocks(call_result.content)

    reason = ''
    required_action = ''
    extra_fields: dict[str, Any] = {}
    if isinstance(structured, dict):
        reason = str(structured.get('reason') or structured.get('message') or '').strip()
        required_action = str(structured.get('required_action') or '').strip()
        if isinstance(structured.get('session_id'), str) and structured['session_id'].strip():
            extra_fields['session_id'] = structured['session_id'].strip()

    if not reason:
        reason = text_blocks[0] if text_blocks else '工具调用参数或执行未通过服务端校验。'
    if not required_action:
        required_action = _default_required_action(tool_name, failure_mode)

    return ToolExecutionResult(
        status='failed',
        failure_mode=failure_mode,
        tool_result={
            'reason': reason,
            'required_action': required_action,
            **extra_fields,
        },
    )


def _build_dependency_error_result(tool_name: str, message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        status='failed',
        failure_mode='dependency',
        tool_result={
            'reason': message,
            'required_action': _default_required_action(tool_name, 'dependency'),
        },
    )


class _MCPFoldablePageStore:
    """为可折叠 MCP 工具管理签名页面引用与有界 LRU 页面块缓存。"""

    def __init__(self, *, capacity: int = 64):
        self._key = secrets.token_bytes(32)
        self._pages: LruCache[tuple[str, str, str, list[dict[str, Any]]]] = LruCache(capacity=capacity)

    def _sign(self, resource_id: str, locator: str, media_type: str, nonce: str) -> str:
        payload = f'{resource_id}\n{locator}\n{media_type}\n{nonce}'.encode()
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def prepare_reference(
        self,
        *,
        resource_id: str,
        locator: str,
        media_type: Literal['text', 'table', 'image'],
        description: str,
        blocks: list[dict[str, Any]],
    ) -> tuple[ToolPageReference, tuple[str, tuple[str, str, str, list[dict[str, Any]]]]]:
        nonce = str(uuid4())
        display_id = f'{nonce}.{self._sign(resource_id, locator, media_type, nonce)}'
        reference = ToolPageReference(
            resource_id=resource_id,
            display_id=display_id,
            locator=locator,
            media_type=media_type,
            description=description,
        )
        return reference, (display_id, (resource_id, locator, media_type, blocks))

    def commit(self, entries: Sequence[tuple[str, tuple[str, str, str, list[dict[str, Any]]]]]) -> None:
        for display_id, value in entries:
            self._pages.put(display_id, value)

    async def resolve_page(self, reference: ToolPageReference) -> list[dict[str, Any]]:
        try:
            nonce, signature = reference.display_id.split('.', 1)
        except ValueError:
            raise ValueError('页面展示标识格式无效') from None
        expected_sig = self._sign(reference.resource_id, reference.locator, reference.media_type, nonce)
        if not hmac.compare_digest(signature, expected_sig):
            raise ValueError('页面展示标识签名无效')
        cached = self._pages.get(reference.display_id)
        if cached is None:
            raise ToolPageUnavailable('该页面内容已从缓存释放，需要核对时请重新调用工具查看。')
        resource_id, locator, media_type, blocks = cached
        if (resource_id, locator, media_type) != (reference.resource_id, reference.locator, reference.media_type):
            raise ValueError('页面引用与缓存元信息不匹配')
        return blocks


def _render_failure_feedback(title: str, execution: ToolExecution) -> ToolExecution:
    result = execution.result.tool_result
    status_label = '依赖不可用' if execution.result.failure_mode == 'dependency' else '执行失败'
    session_line = f"\n会话 ID：{result['session_id']}" if result.get('session_id') else ''
    text = dedent("""
        ### {title}

        状态：{status_label}{session_line}
        原因：{reason}
        下一步：{required_action}
    """).strip().format(
        title=title,
        status_label=status_label,
        session_line=session_line,
        reason=result.get('reason') or '操作未完成。',
        required_action=result.get('required_action') or '请调整参数或换用其他方式处理。',
    )
    return with_feedback(execution, text)


def _render_ordinary_feedback(tool_name: str, arguments, execution: ToolExecution, context) -> ToolExecution:
    title = _TOOL_TITLES.get(tool_name, tool_name)
    if execution.result.status != 'succeeded':
        return _render_failure_feedback(title, execution)

    data = execution.result.tool_result
    if tool_name == 'get_current_time':
        text = dedent("""
            ### 当前时间查询

            状态：查询成功
            当前时间：{current_time}
            时区：{timezone}
            星期：{weekday}
        """).strip().format(
            current_time=data.get('current_time', ''),
            timezone=data.get('timezone', ''),
            weekday=data.get('weekday', ''),
        )
        return with_feedback(execution, text)

    if tool_name == 'calculate_date':
        clamp_line = '\n月末截断：是（目标月份天数不足，已对齐至月末）' if data.get('month_end_clamped') else ''
        text = dedent("""
            ### 日期时间计算

            状态：计算成功
            起始时间：{start_time}
            计算结果：{result_time}
            星期：{weekday}{clamp_line}
        """).strip().format(
            start_time=data.get('start_time', ''),
            result_time=data.get('result_time', ''),
            weekday=data.get('weekday', ''),
            clamp_line=clamp_line,
        )
        return with_feedback(execution, text)

    if tool_name == 'search_web':
        result_id = data.get('result_id')
        count = data.get('count', 0)
        message = data.get('message') or ''
        if result_id:
            text = dedent("""
                ### 网页搜索

                状态：搜索成功
                结果 ID：{result_id}
                命中数量：{count} 条
                下一步：请调用 view_web_search_results 查看搜索结果列表。
            """).strip().format(result_id=result_id, count=count)
        else:
            note_line = f'\n说明：{message}' if message else ''
            text = dedent("""
                ### 网页搜索

                状态：搜索完成，未找到匹配结果
                命中数量：0 条{note_line}
                下一步：请调整关键词或放宽限定条件后重新搜索。
            """).strip().format(note_line=note_line)
        return with_feedback(execution, text)

    if tool_name in {'ask_external_expert', 'continue_external_expert'}:
        reply_content = str(data.get('content') or '').strip()
        text = dedent("""
            ### {title}

            状态：咨询成功
            会话 ID：{session_id}
            当前轮次：第 {round_num} 轮

            {reply_content}
        """).strip().format(
            title=title,
            session_id=data.get('session_id', ''),
            round_num=data.get('round', 1),
            reply_content=reply_content or '外部专家已完成答复。',
        )
        return with_feedback(execution, text)

    # 兜底：若未来扩展普通工具，按非 JSON 文本呈现
    fallback = str(data.get('text') or data.get('message') or '操作已完成。')
    return with_feedback(execution, f'### {title}\n\n状态：执行成功\n\n{fallback}')


def _render_view_search_feedback(arguments, execution: ToolExecution, context) -> ToolExecution:
    if execution.result.status != 'succeeded':
        return _render_failure_feedback(_TOOL_TITLES['view_web_search_results'], execution)
    data = execution.result.tool_result
    next_step = (
        '可省略 page 继续查看下一页，或指定页码重看；使用结果中的 source_id 调用 open_web_page 读取正文。'
        if data.get('has_next')
        else '已到末页；可使用结果中的 source_id 调用 open_web_page 读取正文，或指定页码重看。'
    )
    template = dedent("""
        ### 网页搜索结果查看

        状态：查看记录已提交
        结果 ID：{result_id}
        当前页：第 {page} / {total_pages} 页
        总结果数：{count} 条
        资料状态：{state}
        下一步：{action}
    """).strip()
    values = {
        'result_id': data.get('result_id', ''),
        'page': data.get('page', 1),
        'total_pages': data.get('total_pages', 1),
        'count': data.get('count', 0),
    }
    return with_feedback(
        execution,
        template.format(**values, state='本页搜索结果列表见随后提供的资料。', action=next_step),
        hidden=template.format(
            **values,
            state='本页搜索结果当前未展示，不能从查看记录推断原文。',
            action='需要核对时使用此结果 ID 和页码重新查看；若已过期请重新搜索。',
        ),
        unavailable=template.format(
            **values,
            state='本页搜索结果当前无法读取，不能推断原文。',
            action='请重新调用 view_web_search_results 或重新搜索网页。',
        ),
    )


def _render_open_web_page_feedback(arguments, execution: ToolExecution, context) -> ToolExecution:
    if execution.result.status != 'succeeded':
        return _render_failure_feedback(_TOOL_TITLES['open_web_page'], execution)
    data = execution.result.tool_result
    if data.get('status') == 'no_content':
        no_content_count = data.get('no_content_count', 1)
        next_step = (
            '已多次未提取到可用正文，请停止重复打开无正文页面，改用其他来源或调整检索策略。'
            if isinstance(no_content_count, int) and no_content_count >= 2
            else '请改用其他搜索来源，或换用不同关键词继续检索。'
        )
        message_line = f"\n说明：{data['message']}" if data.get('message') else ''
        text = dedent("""
            ### 网页读取

            状态：未提取到可用正文
            来源 ID：{source_id}
            原因：{reason}{message_line}
            下一步：{next_step}
        """).strip().format(
            source_id=data.get('source_id', ''),
            reason=data.get('reason') or '目标页面未返回可读取的正文内容。',
            message_line=message_line,
            next_step=next_step,
        )
        return with_feedback(execution, text)

    next_step = (
        '可省略 page 继续读取下一页，或指定页码重看。'
        if data.get('has_next')
        else '已到末页；如需核对前文可指定页码重看。'
    )
    image_count = data.get('image_count', 0)
    image_line = f'\n本页配图：{image_count} 张' if isinstance(image_count, int) and image_count > 0 else ''
    notice_line = f"\n图片说明：{data['image_notice']}" if data.get('image_notice') else ''
    template = dedent("""
        ### 网页读取

        状态：读取记录已提交
        来源 ID：{source_id}
        当前页：第 {page} / {total_pages} 页{image_line}{notice_line}
        资料状态：{state}
        下一步：{action}
    """).strip()
    values = {
        'source_id': data.get('source_id', ''),
        'page': data.get('page', 1),
        'total_pages': data.get('total_pages', 1),
        'image_line': image_line,
        'notice_line': notice_line,
    }
    return with_feedback(
        execution,
        template.format(**values, state='本页网页内容见随后提供的资料。', action=next_step),
        hidden=template.format(
            **values,
            state='本页网页内容当前未展示，不能从读取记录推断原文。',
            action='需要核对时使用此来源 ID 与页码重新读取。',
        ),
        unavailable=template.format(
            **values,
            state='本页网页内容当前无法读取，不能推断原文。',
            action='请使用此来源 ID 与页码重新调用 open_web_page。',
        ),
    )


def _dump_call_arguments(arguments: BaseModel) -> dict[str, Any]:
    return arguments.model_dump(exclude_none=True)


def _build_ordinary_executor(client: MCPClientManager, tool_name: str):
    async def execute(arguments: BaseModel) -> ToolExecutionResult:
        try:
            call_result = await client.call_tool(tool_name, _dump_call_arguments(arguments))
        except MCPUnavailableError as error:
            return _build_dependency_error_result(tool_name, str(error))

        if call_result.is_error:
            return _build_mcp_error_result(tool_name, call_result)

        structured = call_result.structured_content if isinstance(call_result.structured_content, dict) else {}
        texts = _extract_text_blocks(call_result.content)

        if tool_name == 'get_current_time':
            return ToolExecutionResult(
                status='succeeded',
                tool_result={
                    'current_time': str(structured.get('current_time') or ''),
                    'timezone': str(structured.get('timezone') or ''),
                    'weekday': str(structured.get('weekday') or ''),
                },
            )
        if tool_name == 'calculate_date':
            return ToolExecutionResult(
                status='succeeded',
                tool_result={
                    'start_time': str(structured.get('start_time') or ''),
                    'result_time': str(structured.get('result_time') or ''),
                    'weekday': str(structured.get('weekday') or ''),
                    'month_end_clamped': bool(structured.get('month_end_clamped', False)),
                },
            )
        if tool_name == 'search_web':
            return ToolExecutionResult(
                status='succeeded',
                tool_result={
                    'count': int(structured.get('count', 0)) if isinstance(structured.get('count'), int) else 0,
                    'result_id': structured.get('result_id') if isinstance(structured.get('result_id'), str) else None,
                    'message': str(structured.get('message') or ''),
                },
            )
        if tool_name in {'ask_external_expert', 'continue_external_expert'}:
            reply_text = str(structured.get('content') or (texts[0] if texts else '')).strip()
            return ToolExecutionResult(
                status='succeeded',
                tool_result={
                    'status': 'success',
                    'session_id': str(structured.get('session_id') or ''),
                    'round': int(structured.get('round', 1)) if isinstance(structured.get('round'), int) else 1,
                    'content': reply_text,
                },
            )
        return ToolExecutionResult(
            status='succeeded',
            tool_result={'text': texts[0] if texts else ''},
        )

    return execute


def _build_view_search_executor(client: MCPClientManager, page_store: _MCPFoldablePageStore):
    async def execute(arguments: BaseModel) -> ToolExecution | ToolExecutionResult:
        try:
            call_result = await client.call_tool('view_web_search_results', _dump_call_arguments(arguments))
        except MCPUnavailableError as error:
            return _build_dependency_error_result('view_web_search_results', str(error))

        if call_result.is_error:
            return _build_mcp_error_result('view_web_search_results', call_result)

        structured = call_result.structured_content if isinstance(call_result.structured_content, dict) else {}
        texts = _extract_text_blocks(call_result.content)
        page_text = texts[0] if texts else str(structured.get('content') or '').strip()
        if not page_text:
            return ToolExecutionResult(
                status='failed',
                failure_mode='failed',
                tool_result={
                    'reason': '搜索结果页面内容为空，无法展示。',
                    'required_action': '请重新查看搜索结果或重新发起网页搜索。',
                },
            )

        result_id = str(structured.get('result_id') or getattr(arguments, 'result_id', ''))
        page = int(structured.get('page', 1)) if isinstance(structured.get('page'), int) else 1
        total_pages = int(structured.get('total_pages', 1)) if isinstance(structured.get('total_pages'), int) else 1
        count = int(structured.get('count', 0)) if isinstance(structured.get('count'), int) else 0

        ref, pending_entry = page_store.prepare_reference(
            resource_id=f'mcp-web-search:{result_id}',
            locator=f'page:{page}',
            media_type='text',
            description=f'网页搜索结果（结果 ID：{result_id}），第 {page}/{total_pages} 页',
            blocks=[{'type': 'text', 'text': page_text}],
        )

        def commit():
            page_store.commit([pending_entry])

        return ToolExecution(
            ToolExecutionResult(
                status='succeeded',
                content=FoldableToolContent(pages=[ref]),
                tool_result={
                    'result_id': result_id,
                    'page': page,
                    'total_pages': total_pages,
                    'count': count,
                    'has_next': page < total_pages,
                },
            ),
            after_commit=commit,
        )

    return execute


def _build_open_web_page_executor(client: MCPClientManager, page_store: _MCPFoldablePageStore):
    async def execute(arguments: BaseModel) -> ToolExecution | ToolExecutionResult:
        try:
            call_result = await client.call_tool('open_web_page', _dump_call_arguments(arguments))
        except MCPUnavailableError as error:
            return _build_dependency_error_result('open_web_page', str(error))

        if call_result.is_error:
            return _build_mcp_error_result('open_web_page', call_result)

        structured = call_result.structured_content if isinstance(call_result.structured_content, dict) else {}
        # 无正文分支不入折叠窗口，走普通文本反馈。
        if structured.get('status') == 'no_content':
            return ToolExecutionResult(
                status='succeeded',
                content=OrdinaryToolContent(),
                tool_result={
                    'status': 'no_content',
                    'has_content': False,
                    'source_id': str(structured.get('source_id') or getattr(arguments, 'source_id', '')),
                    'reason': str(structured.get('reason') or ''),
                    'no_content_count': (
                        int(structured['no_content_count'])
                        if isinstance(structured.get('no_content_count'), int)
                        else 1
                    ),
                    'message': str(structured.get('message') or ''),
                },
            )

        texts = _extract_text_blocks(call_result.content)
        images = _extract_image_blocks(call_result.content)
        page_text = texts[0] if texts else str(structured.get('content') or '').strip()
        if not page_text and not images:
            return ToolExecutionResult(
                status='failed',
                failure_mode='failed',
                tool_result={
                    'reason': '网页读取未返回可展示的正文或图片内容。',
                    'required_action': '请换用其他来源或重新尝试读取。',
                },
            )

        source_id = str(structured.get('source_id') or getattr(arguments, 'source_id', ''))
        focus = str(structured.get('focus') or getattr(arguments, 'focus', '') or '')
        page = int(structured.get('page', 1)) if isinstance(structured.get('page'), int) else 1
        total_pages = int(structured.get('total_pages', 1)) if isinstance(structured.get('total_pages'), int) else 1
        focus_digest = hashlib.sha256(focus.encode()).hexdigest()[:12] if focus else 'default'
        resource_id = f'mcp-web-page:{source_id}:{focus_digest}'

        references: list[ToolPageReference] = []
        pending_entries: list[tuple[str, tuple[str, str, str, list[dict[str, Any]]]]] = []

        if page_text:
            text_ref, text_entry = page_store.prepare_reference(
                resource_id=resource_id,
                locator=f'page:{page}',
                media_type='text',
                description=f'网页正文（来源 {source_id}），第 {page}/{total_pages} 页',
                blocks=[{'type': 'text', 'text': page_text}],
            )
            references.append(text_ref)
            pending_entries.append(text_entry)

        # 第 1 页附带 ImageContent 时按每张图拆分为独立 media_type='image' 页面引用。
        total_images = len(images)
        for idx, image_block in enumerate(images, start=1):
            mime_type = (image_block.mime_type or 'image/png').strip() or 'image/png'
            image_ref, image_entry = page_store.prepare_reference(
                resource_id=resource_id,
                locator=f'page:{page}:image:{idx}',
                media_type='image',
                description=f'网页配图（来源 {source_id}），第 {idx}/{total_images} 张',
                blocks=[{
                    'type': 'image_url',
                    'image_url': {
                        'url': f'data:{mime_type};base64,{image_block.data}',
                        'detail': 'auto',
                    },
                }],
            )
            references.append(image_ref)
            pending_entries.append(image_entry)

        reported_image_count = (
            total_images
            if total_images > 0
            else (int(structured['image_count']) if isinstance(structured.get('image_count'), int) else 0)
        )
        image_notice = structured.get('image_notice') if isinstance(structured.get('image_notice'), str) else None

        def commit():
            page_store.commit(pending_entries)

        return ToolExecution(
            ToolExecutionResult(
                status='succeeded',
                content=FoldableToolContent(pages=references),
                tool_result={
                    'source_id': source_id,
                    'page': page,
                    'total_pages': total_pages,
                    'has_next': page < total_pages,
                    'image_count': reported_image_count,
                    'image_notice': image_notice,
                },
            ),
            after_commit=commit,
        )

    return execute


def _keep_workspace(name, raw, workspace, arguments):
    return workspace


def build_mcp_exploration_tools_from_schemas(
    client: MCPClientManager,
    raw_tools: Sequence[mcp_types.Tool],
) -> list[RegisteredTool]:
    """同步构造器：根据已拉取的 MCP 工具定义构造探索模式 `RegisteredTool` 列表。"""
    tool_map = {tool.name: tool for tool in raw_tools}
    search_page_store = _MCPFoldablePageStore()
    web_page_store = _MCPFoldablePageStore()

    registrations: list[RegisteredTool] = []
    for tool_name in EXPLORATION_MCP_TOOL_NAMES:
        raw_tool = tool_map.get(tool_name)
        if raw_tool is None:
            continue
        description = (raw_tool.description or '').strip()
        if not description:
            raise ValueError(f'MCP 工具 {tool_name} 缺少 description')
        arguments_model = json_schema_to_pydantic_model(tool_name, raw_tool.input_schema)
        progress = extract_tool_progress(raw_tool)

        if tool_name == 'view_web_search_results':
            registrations.append(RegisteredTool(
                name=tool_name,
                description=description,
                arguments_model=arguments_model,
                handler=_keep_workspace,
                executor=_build_view_search_executor(client, search_page_store),
                return_types=('ordinary', 'foldable'),
                page_resolver=search_page_store.resolve_page,
                progress=progress,
                hooks=(FeedbackOnlyHook(_render_view_search_feedback),),
            ))
        elif tool_name == 'open_web_page':
            registrations.append(RegisteredTool(
                name=tool_name,
                description=description,
                arguments_model=arguments_model,
                handler=_keep_workspace,
                executor=_build_open_web_page_executor(client, web_page_store),
                return_types=('ordinary', 'foldable'),
                page_resolver=web_page_store.resolve_page,
                progress=progress,
                hooks=(FeedbackOnlyHook(_render_open_web_page_feedback),),
            ))
        else:
            registrations.append(RegisteredTool(
                name=tool_name,
                description=description,
                arguments_model=arguments_model,
                handler=_keep_workspace,
                executor=_build_ordinary_executor(client, tool_name),
                return_types=('ordinary',),
                progress=progress,
                hooks=(FeedbackOnlyHook(partial(_render_ordinary_feedback, tool_name)),),
            ))

    return registrations


async def build_mcp_exploration_tools(
    client: MCPClientManager,
    *,
    raw_tools: Sequence[mcp_types.Tool] | None = None,
) -> list[RegisteredTool]:
    """从 MCP 客户端拉取 7 个探索工具并封装为 `agent-core` `RegisteredTool` 列表。"""
    tools = await client.list_tools() if raw_tools is None else raw_tools
    return build_mcp_exploration_tools_from_schemas(client, tools)
