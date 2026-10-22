"""程序侧动作提示；不属于模型参数、工具正文或权威业务输出。"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolProgress:
    type: Literal['thinking', 'online-search', 'external-expert'] = 'thinking'
    message: str = '正在思考'

    def __post_init__(self):
        if (self.type not in ('thinking', 'online-search', 'external-expert')
                or not isinstance(self.message, str) or not self.message.startswith('正在')
                or not self.message[2:].strip() or len(self.message) > 2000):
            raise ValueError('工具状态必须是合法类型及不超过2000字符的“正在”动作说明')


DEFAULT_TOOL_PROGRESS = ToolProgress()
