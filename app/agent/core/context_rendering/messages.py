"""按参考项目组织独立摘要、任务和工作区消息，保留原生工具交互。"""
from copy import deepcopy
from ..model import AgentInput
from ..native_messages import validate_native_messages
from ..prompt import SYSTEM_PROMPT
from .history import render_history_messages
from .task import render_task_input
from .workspace import render_workspace_section


def render_current_input(request: AgentInput):
    return render_task_input(request.question,task_number=len(request.context.turns)+1,
        attachments=request.attachments,supplemental=request.context.supplemental,
        platform_permissions=request.platform_permissions,created_at=request.created_at)


def build_fixed_messages(request: AgentInput,*,system_prompt=SYSTEM_PROMPT):
    # 用户问题、附件及补充资料在活动任务记账，固定区只计系统、工具和聊天外壳。
    return [{'role':'system','content':system_prompt},{'role':'user','content':''}]


def build_messages(request: AgentInput,*,workspace=None,summary=None,accepted=(),temporary=(),
                   system_prompt=SYSTEM_PROMPT):
    context=request.context
    if summary is not None:context=context.model_copy(update={'summary':summary,'turns':[]})
    messages=[{'role':'system','content':system_prompt},*render_history_messages(context),
        {'role':'user','content':render_workspace_section(workspace or context.workspace)},
        {'role':'user','content':render_current_input(request)}]
    # 已接受交互的 source 仅供宿主区分，不送给模型；失败反馈独立临时存在。
    native=validate_native_messages(list(accepted))
    messages.extend({key:value for key,value in message.items() if key!='source'} for message in native)
    return deepcopy([*messages,*temporary])
