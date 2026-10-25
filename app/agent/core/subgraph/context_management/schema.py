"""整理子 Agent 的唯一提交工具定义，与主助手工具隔离。"""
from pydantic import Field
from ...workspace import WorkspaceObject,WorkspaceText,WorkspacePayload
from ...tool import check_descriptions


class CompressionArguments(WorkspaceObject):
    summary: WorkspaceText = Field(description='精简的有效资料摘要；保留目标、约束、事实依据、冲突、结论和待办，不新增事实或内部标识。')
    workspace: WorkspacePayload = Field(description='通过保护、引用、状态和容量验收的完整工作区副本。')


class FinishCompressionArguments(WorkspaceObject):
    summary: WorkspaceText = Field(max_length=1000, description='简述实际精简内容及保留的关键事实和保护约束；不提交工作区全文或思维链，不宣称已持久化。最多1000字符，仅在副本严格低于70%时申请完成。')


def definition():
    schema=FinishCompressionArguments.model_json_schema()
    check_descriptions(schema)
    return {'type':'function','function':{'name':'finish_compression','description':'申请完成本次工作区压缩。系统校验当前副本严格低于70%且保护约束满足后才接受；完成说明不替代工作区，未达标必须继续整理。',
        'parameters':schema,'strict':False}}


def compression_tools():
    from ...tool.workspace import build_workspace_registrations
    tools=[entry.definition() for entry in build_workspace_registrations()]+[definition()]
    for tool in tools:check_descriptions(tool['function']['parameters'])
    return tools
