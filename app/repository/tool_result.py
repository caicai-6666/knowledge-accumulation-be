"""事务消费者读取结构化工具结果；旧 JSON 历史只保留只读兼容。"""
import json


def committed_tool_result(content, call):
    stored = getattr(content, 'tool_results', {}).get(call['id'])
    if stored is not None:
        if stored.name != call['function']['name']:
            raise ValueError('工具结构化结果与调用名称不一致')
        return stored.result
    # 仅旧历史缺少独立结果时回退；绝不从新渲染文本提取 ID、时间或写入字段。
    receipt = next((item['content'] for item in content.exchanges
                    if item.get('role') == 'tool' and item.get('tool_call_id') == call['id']), None)
    try:
        result = json.loads(receipt)
    except (TypeError, ValueError):
        raise ValueError('工具缺少可用的结构化提交结果') from None
    if not isinstance(result, dict):
        raise ValueError('工具结构化提交结果必须是对象')
    return result
