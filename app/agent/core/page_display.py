"""通用页面展示窗口；只保存引用，完整返回的主生成响应消耗展示机会。"""
from copy import deepcopy
from .context_rendering.page import render_tool_page_messages, render_tool_receipt
from .tool.result import ToolExecutionResult, ToolPageUnavailable


class PageDisplayWindow:
    def __init__(self, resolver, *, max_rounds=5):
        if not callable(resolver): raise TypeError('页面解析器必须可调用')
        if type(max_rounds) is not int or max_rounds < 1: raise ValueError('页面展示轮数必须为正整数')
        self.max_rounds = max_rounds
        self._resolver = resolver
        self._pending = {}
        self._seen = set()
        self._calls = set()

    def validate(self, call_id, result):
        result = ToolExecutionResult.model_validate(result)
        if result.content.type == 'ordinary': return result
        if not isinstance(call_id, str) or not call_id.strip() or call_id in self._calls:
            raise ValueError('页面必须绑定唯一的成功调用')
        if {ref.display_id for ref in result.content.pages} & self._seen:
            raise ValueError('重新打开页面必须生成新的展示标识')
        return result

    def register(self, call_id, result, *, tool_name):
        result = self.validate(call_id, result)
        if result.content.type == 'ordinary': return
        self._calls.add(call_id)
        self._seen.update(ref.display_id for ref in result.content.pages)
        self._pending[call_id] = (tool_name, result.model_copy(deep=True), self.max_rounds)

    async def inject(self, messages):
        output = []; shown = []
        for message in deepcopy(messages):
            call_id = message.get('tool_call_id') if message.get('role') == 'tool' else None
            if call_id not in self._pending:
                output.append(message)
                continue
            if call_id in shown: raise ValueError('页面回执不能重复')
            name, result, _ = self._pending[call_id]
            visible = {}
            unavailable = set()
            for ref in result.content.pages:
                try:
                    visible[ref.display_id] = await self._resolver(name, ref.model_copy(deep=True))
                except ToolPageUnavailable:
                    # LRU 释放临时图片不应中断整个问答；只标记此资料不可用，保持其他资料与严格类型校验。
                    unavailable.add(ref.display_id)
            rendered = render_tool_page_messages(result, visible_pages=visible)
            message['content'] = render_tool_receipt(result, visible=True, unavailable_pages=unavailable)
            output.extend([message, *rendered])
            shown.append(call_id)
        if set(shown) != set(self._pending): raise ValueError('待展示页面缺少已提交的工具回执')
        return output, tuple(shown)

    def consume(self, shown, *, finish_reason):
        if finish_reason not in ('stop', 'tool_calls'): return
        for call_id in set(shown):
            if call_id not in self._pending: continue
            name, result, remaining = self._pending[call_id]
            if remaining == 1: del self._pending[call_id]
            else: self._pending[call_id] = (name, result, remaining - 1)

    def active_pairs(self, messages):
        """整理保留尚需展示的完整工具对，不能通过丢弃页面绕过容量检查。"""
        output = []
        for index, message in enumerate(messages):
            if message.get('role') == 'assistant':
                call_id = message['tool_calls'][0]['id']
                if call_id in self._pending: output.extend(deepcopy(messages[index:index + 2]))
        return output

    def close(self):
        self._pending.clear()
        self._seen.clear()
        self._calls.clear()
