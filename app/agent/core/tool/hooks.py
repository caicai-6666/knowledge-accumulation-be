"""工具各自注册的执行后钩子；调用上下文只承载私有审计能力。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel
from app.model.node_persistence import GenerationPayload
from .result import ToolExecution


@dataclass(frozen=True)
class ToolHookContext:
    # 每次 execute 单独绑定，禁止把任务或 journal 缓存在共享钩子实例上。
    record_generation: Callable[[str, GenerationPayload], Awaitable[None]] | None = None


ToolHook = Callable[[BaseModel, ToolExecution, ToolHookContext], ToolExecution | Awaitable[ToolExecution]]
