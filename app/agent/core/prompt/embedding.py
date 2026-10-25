"""基础文本向量指令；版本仅供工程记录，不注入模型正文。"""

TEXT_EMBEDDING_VERSION = 'generic-text-retrieval-v1'
TEXT_EMBEDDING_INSTRUCTION = (
    'Represent this text for semantic retrieval. Preserve its topic, entities, relationships, '
    'actions, conditions, negation and uncertainty. Use only information explicitly present.'
)


def render_text_embedding_input(text: str) -> str:
    """复用参考项目的文本协议；保留原文，不加入树、节点或工具执行标识。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('待向量化的文本不能为空')
    return (f'<|im_start|>system\n{TEXT_EMBEDDING_INSTRUCTION}<|im_end|>\n'
            f'<|im_start|>user\n{text}<|im_end|>\n<|im_start|>assistant\n')
