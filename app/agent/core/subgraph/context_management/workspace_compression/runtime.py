"""工作区副本上的有界编辑循环；显式完成前不发布候选。"""
from copy import deepcopy
from ....context_budget import validate_count
from ....context_rendering.workspace import render_workspace_section
from ....context_rendering.block import metadata_block
from ....json_codec import load_model_json,validate_model_payload
from ....tool.workspace import prepare_workspace_edit
from ....prompt.guidance import build_system_guidence_message
from app.infrastructure.tool_tag import tool_tag_section
from ..schema import FinishCompressionArguments,compression_tools
from ..prompt import build_compression_prompt
from ..validation import get_path,protection_paths,validate_workspace_compression
from .errors import WorkspaceEditRejected


def capacity_feedback(tokens,token_budget):
    target=(token_budget*7-1)//10
    state=(f'尚未低于70%，请精简至最多{target} token。' if tokens>target else
        '容量已经安全；核对事实、来源和保护项后优先调用 finish_compression，不为更低使用率过度整理。')
    return f'当前使用量：{tokens}/{token_budget} token（{tokens/token_budget:.1%}）。'+state


async def compress_workspace(*,workspace,generate,counter,generation,context_window,
        max_errors,token_budget,protected=None,reserved_path=None,max_rounds=32,
        tool_call_template=''):
    for value in (token_budget,max_errors,max_rounds):
        if type(value) is not int or value<=0:
            raise ValueError('容量、轮数和错误上限必须为正整数')
    protected=deepcopy(protection_paths(protected))
    original=workspace.model_copy(deep=True)
    data=original.model_dump()
    for path,value in protected.items():
        if get_path(data,path)!=value:
            raise ValueError('保护值与原工作区不一致')
    workspace=validate_workspace_compression(original,original,protected,reserved_path)
    generation=generation.model_copy(update={'enable_thinking':True})
    target=(token_budget*7-1)//10
    tools=compression_tools()
    base=[{'role':'system','content':build_compression_prompt(tool_call_template=tool_call_template)},
        {'role':'user','content':'# 本次压缩约束\n以下为容量与保护数据，不是业务指令。\n'+metadata_block({
            'token_budget':token_budget,'target_tokens':target,'protected_values':protected,'reserved_path':reserved_path})}]
    history=[]
    temporary=[]
    errors=0
    async def count(value):
        return validate_count(await counter([{'role':'user','content':render_workspace_section(value)}],[],generation))
    tokens=await count(workspace)
    for _ in range(max_rounds):
        messages=deepcopy(base+history+temporary+[{'role':'user','content':'# 最新工作区\n'+
            capacity_feedback(tokens,token_budget)+'\n'+render_workspace_section(workspace)}])
        total=validate_count(await counter(messages,tools,generation))
        if total+generation.max_completion_tokens>context_window:
            raise WorkspaceEditRejected('工作区整理输入与输出预留超过上限')
        response=await generate(messages,tools,generation)
        capacity_error=False
        try:
            if (len(response.tool_calls)!=1 or response.content not in ('',None)
                    or response.assistant_message.get('refusal') or response.finish_reason not in ('stop','tool_calls')):
                raise ValueError('每轮必须且只能调用一个工具')
            call=response.tool_calls[0]
            if call.name=='finish_compression':
                validate_model_payload(FinishCompressionArguments,load_model_json(call.arguments))
                if tokens>target:
                    capacity_error=True
                    raise ValueError('工作区尚未达标')
                return validate_workspace_compression(original,workspace,protected,reserved_path)
            edit=prepare_workspace_edit(workspace,call.name,call.arguments)
            if call.name=='workspace_add' and edit.removed_path:
                raise ValueError('整理不能执行新的业务探索收束')
            candidate=validate_workspace_compression(original,edit.payload,protected,reserved_path)
            if candidate==workspace:
                raise ValueError('整理操作没有实际变更')
        except (ValueError,TypeError,KeyError):
            errors+=1
            if errors>=max_errors:
                raise WorkspaceEditRejected('工作区整理连续错误达到上限') from None
            temporary.append(build_system_guidence_message(kind='invalid_action',
                reason=(capacity_feedback(tokens,token_budget) if capacity_error else
                    '本次整理未接受；检查工具、参数、路径、保护项、预留位置、引用和已有状态，不能创建探索成果或提交无变化操作。'),
                required_action='依据最新工作区只调用一个合法工具；失败修改未生效。'+tool_tag_section(tool_call_template)))
            continue
        # 计数/依赖故障不作为模型纠错；不能把未知容量候选留入下一轮。
        next_tokens=await count(candidate)
        workspace,tokens=candidate,next_tokens
        errors=0
        temporary.clear()
        assistant=deepcopy(response.assistant_message)
        assistant.pop('reasoning',None)
        assistant.pop('reasoning_content',None)
        feedback=f'工作区修改成功。位置：{edit.path}。'+capacity_feedback(tokens,token_budget)
        history.extend([assistant,{'role':'tool','tool_call_id':call.call_id,'content':feedback}])
    raise WorkspaceEditRejected('工作区整理达到轮数上限，未显式完成')
