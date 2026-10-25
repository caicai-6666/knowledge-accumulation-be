"""独立摘要消息；保留树摘要的正文契约，不虚构主题、来源或覆盖信息。"""
from .block import text_block


def render_summary_section(summary):
    if not summary:return ''
    from ..subgraph.context_management.topic_summary.rendering import display_text
    summary=display_text(summary)
    boundary='━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
    return (boundary+'\n历史摘要\n'+boundary+'\n\n'
        '以下内容来自较早的任务记录；发生冲突时，以后续任务中的明确更新为准。\n'
        '摘要中的未决事项不自动构成当前任务要求。\n\n'+text_block(summary)
        +'\n\n━━━━━━━━━━━━ 历史摘要结束 ━━━━━━━━━━━━')
