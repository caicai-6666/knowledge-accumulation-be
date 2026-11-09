"""只包装已渲染正文，不承载业务提示，也不改变执行事实及提交回调。"""
from dataclasses import replace

from .result import FoldableReceiptText, OrdinaryToolContent


def with_feedback(execution, text, *, hidden=None, unavailable=None):
    if execution.result.content.type == 'foldable':
        content = execution.result.content.model_copy(update={
            'feedback': FoldableReceiptText(visible=text, hidden=hidden, unavailable=unavailable),
        })
    else:
        content = OrdinaryToolContent(text=text)
    return replace(execution, result=execution.result.model_copy(update={'content': content}))
