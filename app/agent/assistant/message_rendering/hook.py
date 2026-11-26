"""仅注册到助理 finish_task 的执行后钩子；不读取或改写已提交节点。"""
from __future__ import annotations

from dataclasses import replace

from agent_core.core.model import AgentDependencyError
from agent_core.core.tool.hooks import ToolHookContext
from agent_core.core.tool.result import ToolExecution, ToolOperationFailed

from ..tool.finish_task import FinishAssistantTaskArguments
from .schema import MessageRenderingError


class AssistantMessageRenderingHook:
    def __init__(self, agent):
        self.agent = agent

    async def __call__(
        self,
        arguments: FinishAssistantTaskArguments,
        execution: ToolExecution,
        context: ToolHookContext,
    ) -> ToolExecution:
        if execution.result.status != 'succeeded' or not arguments.send_to_user:
            return execution
        # 使用前一个钩子的输出；共享转换器不保存本次任务身份、正文或 journal。
        try:
            if context.record_generation is None:
                raise AgentDependencyError('转换缺少私有生成审计入口，已停止')
            content = await self.agent.render(execution.output, on_generation=context.record_generation)
        except MessageRenderingError as error:
            # 尚未登记正文或交付；返回实际失败，主助手可解释或调整方案，禁止声称已发送。
            raise ToolOperationFailed(
                reason=str(error),
                required_action='本次正文及附件均未提交或发送；可调整完整正文后重试，持续失败时说明交付限制。不要声称用户已收到。',
            ) from None
        return replace(execution, output=content)


__all__ = ['AssistantMessageRenderingHook']
