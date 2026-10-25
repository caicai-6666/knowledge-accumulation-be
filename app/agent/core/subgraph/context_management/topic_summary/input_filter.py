"""摘要只读取业务轨迹的副本；程序指导和私有推理不成为业务记忆。"""
from copy import deepcopy

_SYSTEM_SOURCES = {'fifo', 'workspace', 'system_guidence', 'system_guidance',
                   'system', 'tool_guidance', 'recovery'}


def filter_summary_task(task):
    result = deepcopy(task)
    result.pop('rendered_content', None)
    for field in ('native_messages', 'accepted_interactions', 'messages'):
        if field not in result:
            continue
        messages = []
        for message in result[field]:
            if (message.get('role') not in {'user', 'assistant', 'tool'}
                    or message.get('source') in _SYSTEM_SOURCES
                    or (message.get('role') == 'user' and message.get('source') == 'tool')):
                continue
            message.pop('reasoning', None)
            message.pop('reasoning_content', None)
            messages.append(message)
        result[field] = messages
    # 仅按可信封装字段过滤，不检查正文；用户引用同名标签也必须保留。
    return result
