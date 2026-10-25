"""探索中途输出：提交与交付后继续执行，不结束任务或等待用户。"""
from pydantic import Field
from app.agent.core.workspace import WorkspaceObject, WorkspaceText
from app.agent.core.tool.registry import RegisteredTool


class ProgressArguments(WorkspaceObject):
    content: WorkspaceText = Field(description='直接展示给用户的完整中途正文，用于阶段发现、解释或下一步安排；清楚区分已有依据、已完成事实、计划和具体限制。不提交私有推理草稿，不把部分完成说成全部完成。')


def build_progress_registration():
    def prepare(name, raw, workspace, arguments):
        return workspace
    return RegisteredTool('emit_progress','当你需要向用户报告阶段性发现、处理进展或接下来的操作安排，同时还要继续执行当前任务时，使用这个工具。展示中途内容；成功后继续执行，本轮任务不结束，也不暂停等待用户。',ProgressArguments,prepare,output_kind='intermediate')
