"""必须由业务模式继承实现的工具契约，不提供默认业务行为。"""
from .finish_task import FinishTaskTool, OutputArguments

__all__ = ["FinishTaskTool", "OutputArguments"]
