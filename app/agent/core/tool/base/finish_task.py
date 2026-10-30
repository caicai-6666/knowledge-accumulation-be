"""最终输出的抽象契约；具体交付语义由助理或探索模式定义。"""
from abc import ABC, abstractmethod
from pydantic import Field
from ...workspace import WorkspaceObject, WorkspaceText


class OutputArguments(WorkspaceObject):
    content: WorkspaceText = Field(description='直接展示给用户的完整正文，清楚区分已有依据、已完成事实、计划和具体限制。用于本轮答复或必须由用户回答的澄清问题。不提交私有推理草稿，不把部分完成说成全部完成。')


class FinishTaskTool(ABC):
    arguments_model = OutputArguments
    deliver_output = None

    def __init__(self, *, hooks=()):
        self.hooks = tuple(hooks)

    @property
    @abstractmethod
    def description(self) -> str:
        """由模式说明何时结束任务，以及最终内容如何交付。"""
        raise NotImplementedError

    def build_registration(self):
        from ..registry import RegisteredTool
        if not isinstance(self.arguments_model, type) or not issubclass(self.arguments_model, OutputArguments):
            raise TypeError('finish_task 参数必须继承 OutputArguments')
        return RegisteredTool(
            'finish_task', self.description, self.arguments_model,
            lambda name, raw, workspace, arguments: workspace,
            output_kind='final', deliver_output=self.deliver_output, hooks=self.hooks,
        )
