"""摘要内部结构与模型可见正文分离；不向模型泄露树节点或落盘元信息。"""
from copy import deepcopy
from ....json_codec import load_model_json
from ....context_rendering.block import metadata_block, text_block
from .schema import TopicMemory, TopicSummary, SummaryItem
from .input_filter import filter_summary_task


def read_memory(text):
    if not text:
        return TopicMemory(topics=[])
    try:
        value = load_model_json(text)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict) and value.get('format') == 'topic_memory':
        # 自身格式损坏必须失败，不能退化为普通正文后掩盖错误。
        return TopicMemory.model_validate(value)
    # 当前数据库已有的纯文本摘要作为一个旧来源保留，不虚构事实拆分或出处。
    return TopicMemory(topics=[TopicSummary(topic_id='previous_memory', title='既有记忆',
        current_memory=[SummaryItem(content=text, task_ids=[], sources=[])], open_items=[])])


def memory_text(memory):
    lines = []
    for topic in memory.topics:
        lines.extend([f'### {topic.title}'])
        for label, items in (('当前记忆', topic.current_memory), ('未决事项', topic.open_items)):
            lines.append(f'#### {label}')
            lines.extend('- '+item.content+('\n  来源：'+'；'.join(item.sources) if item.sources else '') for item in items)
            if not items:
                lines.append('（无）')
    return '\n\n'.join(lines) or '没有需要保留的历史记忆。'


def display_text(text):
    """旧纯文本原样展示；新结构仅投影业务正文，不改变外部接口的text语义。"""
    try:
        value = load_model_json(text)
    except (ValueError, TypeError):
        return text
    if isinstance(value, dict) and value.get('format') == 'topic_memory':
        return memory_text(read_memory(text))
    return text


def prepare_sources(source):
    history = source.get('history', source)
    previous = {t.topic_id: t for t in read_memory(history.get('summary')).topics}
    tasks = {f'task_{i}': deepcopy(turn) for i, turn in enumerate(history.get('questions_and_answers', []), 1)}
    if 'current_question' in source:
        tasks[f'task_{len(tasks)+1}'] = {'question': source['current_question'],
            'created_at': source.get('current_task_created_at'),
            'accepted_interactions': deepcopy(source.get('accepted_interactions', []))}
    return {key: filter_summary_task(task) for key, task in tasks.items()}, previous


def render_material(tasks, previous):
    parts = ['## 旧记忆']
    for key, topic in previous.items():
        # 旧条目的 task_ids 属于此前整理，绝不冒充本次任务来源。
        body = topic.model_dump(exclude={'topic_id'})
        for region in ('current_memory', 'open_items'):
            for item in body[region]:
                item.pop('task_ids')
        parts.extend([f'旧主题来源：{key}', metadata_block(body)])
    if not previous:
        parts.append('（无）')
    parts.append('## 本次有效任务资料（按发生顺序）')
    for key, task in tasks.items():
        parts.extend([f'任务来源：{key}', metadata_block(task)])
    if not tasks:
        parts.append('（无）')
    return '\n\n'.join(parts)


def render_topic(topic):
    return '## 本次整理主题\n'+text_block(topic.title)+'\n提取范围：\n'+text_block(topic.scope)
