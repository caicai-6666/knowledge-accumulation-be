"""输出工具的实时正文预览；预览不进入权威上下文，提交后才确认完成。"""
import json
import re

from app.model.question_tree import new_tree_id


_PREFIX = re.compile(r'^\s*\{\s*"content"\s*:\s*"')


def content_prefix(arguments):
    """沿用参考项目的增量 JSON 字符串解码，不猜补转义或 UTF-16 代理对。"""
    match = _PREFIX.match(arguments)
    if match is None:
        return ''
    result, index = [], match.end()
    while index < len(arguments):
        char = arguments[index]
        if char == '"':
            break
        if char == '\\':
            if index + 1 >= len(arguments):
                break
            size = 6 if arguments[index + 1] == 'u' else 2
            if index + size > len(arguments):
                break
            try:
                value = json.loads('"' + arguments[index:index + size] + '"')
                if len(value) == 1 and 0xD800 <= ord(value) <= 0xDBFF:
                    if index + 12 > len(arguments):
                        break
                    size = 12
                    value = json.loads('"' + arguments[index:index + size] + '"')
                if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                    break
            except (ValueError, TypeError):
                break
            result.append(value)
            index += size
        elif ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF:
            break
        else:
            result.append(char)
            index += 1
    return ''.join(result)


class InteractionOutputStream:
    """每次生成独立展示 ID；数据库提交使用同一 ID，失败预览只能标记中断。"""
    def __init__(self, publish, *, output_kinds):
        self.publish = publish
        self.output_kinds = dict(output_kinds)
        self.reset()

    def reset(self):
        self.message_id = new_tree_id()
        self.call_id = self.name = self.kind = None
        self.text = ''
        self.active = self.blocked = self.committing = False

    async def begin(self):
        await self.interrupt()
        self.reset()

    async def on_delta(self, message):
        if self.blocked:
            return
        calls = message.get('tool_calls', ())
        if len(calls) > 1 or message.get('content') or message.get('refusal'):
            await self.interrupt()
            return
        if not calls:
            return
        call = calls[0]
        name = call['function']['name']
        if self.name is not None and (name != self.name or call['id'] != self.call_id):
            await self.interrupt()
            return
        if name not in self.output_kinds or not call['id']:
            return
        self.call_id, self.name = call['id'], name
        self.kind = self.output_kinds[name]
        text = content_prefix(call['function']['arguments'])
        if not text.startswith(self.text):
            await self.interrupt()
            return
        await self.extend(text)

    async def extend(self, text):
        while len(self.text) < len(text):
            delta = text[len(self.text):len(self.text) + 512]
            # 状态先记录，回调取消时宿主可能已经发布；清理仍能收束同一条预览。
            self.text += delta
            self.active = True
            await self.publish('message.delta', {'message_id': self.message_id,
                'message_kind': self.kind, 'delta': delta})

    async def interrupt(self):
        # 提交回执不明或交付被取消时由宿主回查 SQL，不能先把已落库答案标成中断。
        if self.active and not self.committing:
            await self.publish('message.completed', {'message_id': self.message_id,
                'message_kind': self.kind, 'text': self.text, 'status': 'interrupted'})
            self.active = False
        self.blocked = True

    async def prepare_commit(self, call, text):
        kind = self.output_kinds[call.name]
        if self.active and (self.call_id != call.call_id or self.kind != kind or not text.startswith(self.text)):
            await self.interrupt()
        if self.blocked or self.call_id != call.call_id:
            self.reset()
        self.call_id, self.kind = call.call_id, kind
        self.committing = True
        return self.message_id

    async def complete(self, text):
        await self.extend(text)
        await self.publish('message.completed', {'message_id': self.message_id,
            'message_kind': self.kind, 'text': text, 'status': 'completed'})
        self.active = False
