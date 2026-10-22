"""中途输出与最终输出的参数和注册；实际提交由宿主 journal 负责。"""
from pydantic import Field
from ..workspace import WorkspaceObject,WorkspaceText

class OutputArguments(WorkspaceObject):
    content: WorkspaceText = Field(description='直接展示给用户的完整正文，清楚区分已有依据、已完成事实、计划和具体限制。中途输出用于阶段发现、解释或下一步安排；最终输出用于本轮答复或必须由用户回答的澄清问题。不提交私有推理草稿，不把部分完成说成全部完成。')


def build_interaction_registrations():
    from .registry import RegisteredTool
    def prepare(name,raw,workspace,arguments):
        return workspace
    return [
        RegisteredTool('emit_progress','当你需要向用户报告阶段性发现、处理进展或接下来的操作安排，同时还要继续执行当前任务时，使用这个工具。展示中途内容；成功后继续执行，本轮任务不结束，也不暂停等待用户。',OutputArguments,prepare),
        RegisteredTool('finish_task','当你需要提交最终答复，或因缺少必要信息而需要用户补充后才能继续时，使用这个工具。向用户提交最终反馈并正式结束本轮任务；可交付答案、说明限制或请求必要信息，成功后不再继续本轮调用。',OutputArguments,prepare),
    ]
