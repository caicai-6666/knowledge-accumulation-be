"""主助手可见的上下文整理入口；与整理子 Agent 的内部工具分开。"""
from dataclasses import replace
from textwrap import dedent

from pydantic import Field

from ..workspace import WorkspaceObject, WorkspaceText
from .hooks import ToolHookContext
from .result import OrdinaryToolContent, ToolExecution


class CompactArguments(WorkspaceObject):
    focus: WorkspaceText = Field(description='用自然语言说明整理时优先保留的任务目标、事实依据、已有结论、冲突和未决问题，例如“保留两种方案的交付周期、来源及尚未核实的风险”。只说明保留重点，不在这里新增事实、改写用户要求或填写内部编号。')


def render_context_feedback(
    arguments: CompactArguments,
    execution: ToolExecution,
    context: ToolHookContext,
) -> ToolExecution:
    # 整理与验收由宿主完成；钩子只展示确认后的范围，不重新生成摘要。
    if execution.result.status != 'succeeded':
        return execution

    if context.compacted_turns is None:
        text = dedent("""
            ### 上下文整理

            状态：等待宿主执行整理并提交，尚不能确认完成。
        """).strip()
    else:
        text = dedent(f"""
            ### 上下文整理

            状态：整理成功
            已整理历史任务：{context.compacted_turns} 条
            保留范围：未纳入整理的历史任务及本轮活动记录。
            下一步：依据更新后的摘要和工作区继续当前任务。
        """).strip()

    return replace(
        execution,
        result=execution.result.model_copy(
            update={'content': OrdinaryToolContent(text=text)},
        ),
    )


def build_context_registrations():
    from .registry import RegisteredTool

    def prepare(name, raw, workspace, arguments):
        return workspace

    return [
        RegisteredTool(
            'compact_context',
            (
                '当你需要收束较长的历史并保留继续任务所需的信息时，使用这个工具。'
                '按关注重点整理指定的完整历史任务前缀并合并既有摘要，保留用户目标、事实依据、来源、冲突和待办；'
                '剩余历史和本轮活动轨迹原样保留，不结束本轮。仅有本轮任务而没有完整历史时不能使用。'
                '整理不能修改用户约束、条目身份或事实确认状态；需要删除工作区条目时使用工作区删除工具。'
            ),
            CompactArguments,
            prepare,
            hooks=(render_context_feedback,),
        ),
    ]
