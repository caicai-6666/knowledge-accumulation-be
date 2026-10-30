"""仅注册到助理 finish_task 的执行后钩子；不读取或改写已提交节点。"""
from dataclasses import replace

from app.agent.core.tool import ToolExecution, ToolHookContext
from ..tool.finish_task import FinishAssistantTaskArguments
from .schema import MessageRenderingError


class AssistantMessageRenderingHook:
    def __init__(self, agent):
        self.agent = agent

    async def __call__(self, arguments: FinishAssistantTaskArguments,
                       execution: ToolExecution, context: ToolHookContext) -> ToolExecution:
        if execution.result.status != 'succeeded' or not arguments.send_to_user:
            return execution
        # 使用前一个钩子的输出；共享转换器不保存本次任务身份、正文或 journal。
        try:
            if context.record_generation is None:
                raise MessageRenderingError('转换缺少私有生成审计入口，尚未转换或提交；需绑定当前任务审计。')
            content = await self.agent.render(execution.output, on_generation=context.record_generation)
        except MessageRenderingError as error:
            # 安全定向原因已由转换校验就近提供；终止主循环，不能触发主助理纠错重放。
            from app.agent.core.runtime import AgentCoreError
            raise AgentCoreError(str(error)) from None
        return replace(execution, output=content)
