"""工具独立的执行后钩子；上下文提供宿主事实与私有审计能力。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable

from pydantic import BaseModel

from app.model.node_persistence import GenerationPayload

from .result import ToolExecution


@dataclass(frozen=True)
class ToolHookContext:
    # 每次 execute 单独绑定，禁止把任务或 journal 缓存在共享钩子实例上。
    record_generation: Callable[[str, GenerationPayload], Awaitable[None]] | None = None

    # 宿主已完成候选验收与容量计算；正文仍须随成功提交后才能展示。
    workspace_feedback: str | None = None

    # 宿主完成摘要验收后提供的完整历史任务数量；不含本轮活动轨迹。
    compacted_turns: int | None = None


ToolHook = Callable[
    [BaseModel, ToolExecution, ToolHookContext],
    ToolExecution | Awaitable[ToolExecution],
]


@dataclass(frozen=True)
class FeedbackOnlyHook:
    """只允许替换反馈内容，保证最终正文不变，因而不阻断既有流式预览。"""

    render: ToolHook

    async def __call__(self, arguments, execution, context):
        original = execution.result.model_copy(deep=True)
        value = self.render(arguments, execution, context)
        value = await value if isawaitable(value) else value
        if (not isinstance(value, ToolExecution)
                or value.output != execution.output
                or value.after_commit is not execution.after_commit
                or value.result.status != original.status
                or value.result.tool_result != original.tool_result
                or value.result.system_guidence != original.system_guidence):
            raise ValueError('反馈专用钩子只能修改展示内容')
        return value
