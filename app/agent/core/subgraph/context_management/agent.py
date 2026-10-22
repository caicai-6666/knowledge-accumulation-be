"""受 token 预算和有限纠错约束的整理子 Agent。"""
from pydantic import ValidationError
from ...json_codec import load_model_json,render_json,validate_model_payload
from ...context_budget import validate_count
from ...context_rendering.workspace import render_workspace_section
from .prompt import build_compression_prompt
from app.infrastructure.tool_tag import tool_tag_section
from .schema import CompressionArguments,definition
from .validation import validate_workspace_compression


async def compress(*,source,workspace,focus,generate,counter,generation,context_window,
                   max_errors,workspace_limit,summary_limit,protected=None,tool_call_template=''):
    tools=[definition()]
    messages=[{'role':'system','content':build_compression_prompt(tool_call_template=tool_call_template)},{'role':'user','content':render_json({
        'material':source,'workspace':workspace.model_dump(),'focus':focus,'protected_entries':protected or {}})}]
    for _ in range(max_errors):
        count=validate_count(await counter(messages,tools,generation))
        if count+generation.max_completion_tokens>context_window:
            raise ValueError('压缩输入与输出预留超过上下文上限')
        response=await generate(messages,tools,generation)
        try:
            if (len(response.tool_calls)!=1 or response.content not in ('',None)
                    or response.assistant_message.get('refusal') or response.finish_reason not in ('stop','tool_calls')):
                raise ValueError('整理必须提交完整的唯一工具调用')
            call=response.tool_calls[0]
            if call.name!='finish_compression':
                raise ValueError('整理工具不合法')
            result=validate_model_payload(CompressionArguments,load_model_json(call.arguments))
            validate_workspace_compression(workspace,result.workspace,protected)
            # 使用同一模型分词器验收，不以字符数冒充 token。
            wc=validate_count(await counter([{'role':'user','content':render_workspace_section(result.workspace)}],[],generation))
            sc=validate_count(await counter([{'role':'user','content':result.summary}],[],generation))
            if wc>=workspace_limit or sc>=summary_limit:
                raise ValueError('整理结果未达到容量目标')
            return result
        except (ValueError,TypeError) as error:
            reason=('字段 '+ '、'.join('.'.join(map(str,e['loc'])) or '整体参数' for e in error.errors()[:4])+' 不符合 Schema'
                if isinstance(error,ValidationError) else '条目身份、用户原始任务、来源、状态和引用必须保持不变，结果必须达到容量目标')
            section=tool_tag_section(tool_call_template)
            messages.append({'role':'user','content':reason+'。请只提交 finish_compression，原样保留用户任务与补充、所有键和引用，只精简正文措辞。'+('\n'+section if section else '')})
    raise ValueError('整理连续未通过验收，停止执行')
