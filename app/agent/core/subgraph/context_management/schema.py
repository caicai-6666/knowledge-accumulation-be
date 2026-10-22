"""整理子 Agent 的唯一提交工具定义，与主助手工具隔离。"""
from pydantic import Field
from ...workspace import WorkspaceObject,WorkspaceText,WorkspacePayload
from ...tool import check_descriptions


class CompressionArguments(WorkspaceObject):
    summary: WorkspaceText = Field(description='精简的有效资料摘要；保留目标、约束、事实依据、冲突、结论和待办，不新增事实或内部标识。')
    workspace: WorkspacePayload = Field(description='整理后的完整工作区，保留所有稳定条目、用户约束、来源、状态和引用，仅精简正文与规划措辞。')


def definition():
    schema=CompressionArguments.model_json_schema()
    check_descriptions(schema)
    return {'type':'function','function':{'name':'finish_compression','description':'当你已经完成资料摘要和工作区精简，需要提交本次整理结果时，使用这个工具。一次提交有效资料摘要及整理后的完整工作区；保留用户约束、稳定条目、事实来源、确认状态和引用。提交后等待验收，不把新增事实或尚未完成的工作写成已有结论。',
        'parameters':schema,'strict':False}}
