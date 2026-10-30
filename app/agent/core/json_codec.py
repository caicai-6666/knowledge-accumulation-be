"""模型 JSON 传输兼容；沿用参考项目的 Schema 定向解码，不修复语法。"""
import json
from math import isfinite
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

Model = TypeVar('Model', bound=BaseModel)


class ModelActionRejected(ValueError):
    """仅传递校验位置提供的安全反馈；此类不维护业务提示文案。"""

    def __init__(self, reason: str, required_action: str):
        super().__init__(reason)
        self.reason = reason
        self.required_action = required_action


def model_validation_feedback(error: ValidationError, model: type[BaseModel]) -> ModelActionRejected:
    """按可信 Schema 格式化字段约束，不带原始输入、任意键名或异常消息。"""
    issues = error.errors(include_input=False, include_url=False)
    for issue in issues:
        specific = issue.get('ctx', {}).get('error')
        if isinstance(specific, ModelActionRejected):
            return specific
    root = model.model_json_schema()
    details = []
    for issue in issues[:5]:
        schema, path = root, []
        for part in issue['loc']:
            while '$ref' in schema:
                schema = root['$defs'][schema['$ref'].rsplit('/', 1)[1]]
            if isinstance(part, str) and part in schema.get('properties', {}):
                path.append(part)
                schema = schema['properties'][part]
            elif isinstance(part, int) and schema.get('type') == 'array':
                path.append('[]')
                schema = schema.get('items', {})
            else:
                break
        rule = {'missing': '缺少必填字段', 'extra_forbidden': '包含未定义字段',
                'string_type': '需要字符串', 'int_type': '需要整数',
                'bool_type': '需要布尔值', 'dict_type': '需要对象',
                'list_type': '需要数组', 'literal_error': '不在允许值中'}.get(issue['type'], '不符合字段约束')
        # 所有值来自代码中的 Schema；不读取 issue.msg、input 或任意 ctx 文本。
        constraints = {key: schema[key] for key in ('type', 'enum', 'const', 'minimum',
            'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'minLength', 'maxLength',
            'minItems', 'maxItems', 'pattern') if key in schema}
        details.append(('.'.join(path) or '参数对象')+'：'+rule+'；'+
                       schema.get('description', '')+' '+render_json(constraints))
    return ModelActionRejected('参数校验未通过：'+'；'.join(details),
        '按上述字段说明和当前 Schema 补全或修正参数，移除未定义字段；不要编造缺失事实。')


def load_model_json(raw: str) -> Any:
    """严格读取一层 JSON，内嵌层同样拒绝重复键和非标准数值。"""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ModelActionRejected('JSON 对象包含重复字段。', '每个对象字段只保留一次，重新提交完整 JSON；不要追加第二份同名字段。')
            result[key] = value
        return result

    def invalid(value):
        raise ModelActionRejected('JSON 包含 NaN 或 Infinity 等非标准数值。', '使用有限 JSON 数字；只有 Schema 允许空值时才可使用 null。')

    def finite_float(value):
        number = float(value)
        if not isfinite(number):
            raise ModelActionRejected('JSON 数字超出有限范围。', '改用可表示的有限数值，不能使用无穷大或超大指数绕过范围约束。')
        return number

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite_float)
    except json.JSONDecodeError:
        raise ModelActionRejected('JSON 语法不完整或不合法。', '提交完整合法的 JSON，使用双引号，不带 Markdown 围栏、注释或尾随逗号。') from None


def normalize_model_json(value: Any, schema: dict, *, root: dict | None = None, depth: int = 0) -> Any:
    """仅对期望对象/数组的字段解码；字符串及无类型字段原样保留。

    union 允许字符串时优先保留文本；调用方可依据业务判别字段缩小 Schema。
    返回新容器，不改写原始响应或私有审计。最终合法性仍由原校验器负责。
    """
    if depth > 64:
        raise ModelActionRejected('JSON 嵌套过深。', '简化参数层级，按 Schema 提交必要结构，较长内容分次处理。')
    root = schema if root is None else root
    if '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/'):
            raise ValueError('模型 JSON Schema 仅支持本地引用')
        resolved = root
        for part in ref[2:].split('/'):
            resolved = resolved[part.replace('~1', '/').replace('~0', '~')]
        return normalize_model_json(value, resolved, root=root, depth=depth + 1)
    branches = schema.get('anyOf', schema.get('oneOf'))
    if branches:
        # 展开引用以判断允许的 JSON 类型；不猜测字符串究竟是否意在提交对象。
        def kind(branch):
            if '$ref' in branch:
                target = root
                for part in branch['$ref'][2:].split('/'):
                    target = target[part.replace('~1', '/').replace('~0', '~')]
                return target.get('type')
            return branch.get('type')
        if isinstance(value, str) and any(kind(b) in (None, 'string') for b in branches):
            return value
        if isinstance(value, str) and any(kind(b) in ('object', 'array') for b in branches):
            decoded = load_model_json(value)
            return normalize_model_json(decoded, schema, root=root, depth=depth + 1)
        expected = 'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else None
        candidates = [b for b in branches if expected and kind(b) == expected]
        if len(candidates) == 1:
            return normalize_model_json(value, candidates[0], root=root, depth=depth + 1)
        # 多个对象分支只在产生一致结果时采用转换，避免擅自选中错误分支。
        if candidates:
            results = [normalize_model_json(value, b, root=root, depth=depth + 1) for b in candidates]
            if all(result == results[0] for result in results):
                return results[0]
        return value
    expected = schema.get('type')
    if isinstance(value, str) and expected in ('object', 'array'):
        return normalize_model_json(load_model_json(value), schema, root=root, depth=depth + 1)
    if isinstance(value, dict) and expected == 'object':
        properties = schema.get('properties', {})
        additional = schema.get('additionalProperties', {})
        return {key: normalize_model_json(item, properties.get(key, additional) if isinstance(
            properties.get(key, additional), dict) else {}, root=root, depth=depth + 1)
            for key, item in value.items()}
    if isinstance(value, list) and expected == 'array':
        prefix = schema.get('prefixItems', [])
        return [normalize_model_json(item, prefix[index] if index < len(prefix) else schema.get('items', {}),
                    root=root, depth=depth + 1) for index, item in enumerate(value)]
    return value


def validate_model_payload(model: type[Model], payload: Any, **kwargs) -> Model:
    """用于工具参数：兼容传输后继续执行原 Pydantic Python 校验。"""
    return model.model_validate(normalize_model_json(payload, model.model_json_schema()), **kwargs)


def validate_model_json(model: type[Model], raw: str, **kwargs) -> Model:
    """用于强制 JSON 输出：保留原有 JSON 模式（如严格 tuple/date）的语义。"""
    payload = normalize_model_json(load_model_json(raw), model.model_json_schema())
    return model.model_validate_json(json.dumps(payload, ensure_ascii=False, allow_nan=False), **kwargs)


def render_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
