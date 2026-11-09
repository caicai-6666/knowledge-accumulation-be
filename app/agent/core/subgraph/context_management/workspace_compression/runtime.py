"""工作区副本上的有界编辑循环；显式完成前不发布候选。"""
from copy import deepcopy
from dataclasses import dataclass, replace
from textwrap import dedent
from pydantic import ValidationError
from ....context_budget import validate_count
from ....context_rendering.workspace import render_workspace_section
from ....context_rendering.block import metadata_block
from ....json_codec import load_model_json,validate_model_payload,ModelActionRejected,model_validation_feedback
from ....tool.workspace import prepare_workspace_edit, _MODELS, build_workspace_registrations
from ....tool.hooks import ToolHookContext
from ....tool.registry import ToolRegistry
from ....tool.result import OrdinaryToolContent, ToolExecution
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


@dataclass(frozen=True)
class CompressionFeedbackContext(ToolHookContext):
    edited_path: str = ''
    capacity: str = ''


def render_compression_edit_feedback(
    arguments,
    execution: ToolExecution,
    context: CompressionFeedbackContext,
) -> ToolExecution:
    # 此时仅更新本次整理副本，不能把副本编辑描述为宿主工作区已提交。
    text = dedent("""
        ### 工作区副本编辑

        状态：修改成功，仅更新整理副本。
        位置：{path}
        {capacity}
    """).strip().format(path=context.edited_path, capacity=context.capacity)
    return replace(
        execution,
        result=execution.result.model_copy(
            update={'content': OrdinaryToolContent(text=text)},
        ),
    )


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
    # 每个编辑工具独立替换为副本专用钩子，不复用主助理的提交成功文案。
    feedback_registry = ToolRegistry([
        replace(entry, hooks=(render_compression_edit_feedback,))
        for entry in build_workspace_registrations()
    ])
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
        argument_model=None
        try:
            if response.assistant_message.get('refusal'):
                raise ModelActionRejected('本轮整理响应包含拒绝标记，未接受修改。', '不绕过拒绝；只在允许范围内整理已有材料。')
            if response.finish_reason not in ('stop','tool_calls'):
                raise ModelActionRejected('整理调用没有完整生成，未接受修改。', '缩短参数，重新提交一个完整工作区调用或 finish_compression。')
            if len(response.tool_calls)!=1:
                raise ModelActionRejected('本轮未提交恰好一个整理工具调用，未接受修改。', '每轮只调用一个 workspace_add、workspace_replace、workspace_delete 或 finish_compression；普通说明不能代替调用。')
            call=response.tool_calls[0]
            if call.name=='finish_compression':
                argument_model=FinishCompressionArguments
                validate_model_payload(FinishCompressionArguments,load_model_json(call.arguments))
                if tokens>target:
                    raise ModelActionRejected(capacity_feedback(tokens,token_budget), '继续精简未受保护的重复内容，达到目标后再调用 finish_compression。')
                return validate_workspace_compression(original,workspace,protected,reserved_path)
            if call.name not in _MODELS:
                raise ModelActionRejected('该工具不属于本次可用的整理工具。', '只使用工作区编辑工具；完成后调用 finish_compression，不开展新业务探索。')
            argument_model=_MODELS[call.name]
            edit=prepare_workspace_edit(workspace,call.name,call.arguments)
            if call.name=='workspace_add' and edit.removed_path:
                raise ModelActionRejected('整理不能新增探索成果或收束剩余方向。', '仅精简已有信息、规划或结论，保留原有探索状态。')
            candidate=validate_workspace_compression(original,edit.payload,protected,reserved_path)
            if candidate==workspace:
                raise ModelActionRejected('本次整理没有实际改变工作区。', '选择能减少冗余的有效编辑；容量已达标时核对保护项后调用 finish_compression。')
        except (ValueError,TypeError,KeyError) as error:
            errors+=1
            if errors>=max_errors:
                raise WorkspaceEditRejected('工作区整理连续错误达到上限',
                    reason='整理过程中连续调用未通过校验，整理副本未提交，旧工作区保持不变。',
                    required_action='缩小本次修改或精简现有内容，不把未完成的整理当作成功；不要机械重试同一操作。') from None
            if isinstance(error,ValidationError):
                # 编辑后的完整状态失败按工作区 Schema 定位，而非误指向工具参数。
                from ....workspace import WorkspacePayload
                error=model_validation_feedback(error,argument_model if argument_model and error.title==argument_model.__name__ else WorkspacePayload)
            reason=(error.reason if isinstance(error,ModelActionRejected) else '本次整理未通过校验，修改未接受。')
            action=(error.required_action if isinstance(error,ModelActionRejected) else '依据最新工作区和已提供的保护约束选择合法编辑，不能假定本次修改已生效。')
            temporary.append(build_system_guidence_message(kind='invalid_action',reason=reason,
                required_action=action+tool_tag_section(tool_call_template)))
            continue
        # 计数/依赖故障不作为模型纠错；不能把未知容量候选留入下一轮。
        next_tokens=await count(candidate)
        execution = await feedback_registry.execute(
            call.name,
            feedback_registry.entries[call.name].parser(call.arguments),
            hook_context=CompressionFeedbackContext(
                edited_path=edit.path,
                capacity=capacity_feedback(next_tokens, token_budget),
            ),
        )
        workspace,tokens=candidate,next_tokens
        errors=0
        temporary.clear()
        assistant=deepcopy(response.assistant_message)
        # 附带文字只留私有生成审计，后续上下文只提交成功的规范化工具对。
        assistant['content']=None
        assistant.pop('reasoning',None)
        assistant.pop('reasoning_content',None)
        feedback = execution.result.content.text
        history.extend([assistant,{'role':'tool','tool_call_id':call.call_id,'content':feedback}])
    raise WorkspaceEditRejected('工作区整理达到轮数上限，未显式完成')
