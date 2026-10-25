"""主助手可见的上下文整理入口；与整理子 Agent 的内部工具分开。"""
from pydantic import Field
from ..workspace import WorkspaceObject,WorkspaceText

class CompactArguments(WorkspaceObject):
    focus: WorkspaceText = Field(description='用自然语言说明整理时优先保留的任务目标、事实依据、已有结论、冲突和未决问题，例如“保留两种方案的交付周期、来源及尚未核实的风险”。只说明保留重点，不在这里新增事实、改写用户要求或填写内部编号。')


def build_context_registrations():
    from .registry import RegisteredTool
    def prepare(name,raw,workspace,arguments):
        return workspace
    return [RegisteredTool('compact_context','当你需要收束较长的历史并保留继续任务所需的信息时，使用这个工具。按关注重点整理指定的完整历史任务前缀并合并既有摘要，保留用户目标、事实依据、来源、冲突和待办；剩余历史和本轮活动轨迹原样保留，不结束本轮。仅有本轮任务而没有完整历史时不能使用。整理不能修改用户约束、条目身份或事实确认状态；需要删除工作区条目时使用工作区删除工具。',CompactArguments,prepare)]
