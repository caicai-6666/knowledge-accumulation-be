"""有限多轮查询；失败轨迹只参与连续纠错，原生生成及反馈另存私有审计。"""
from copy import deepcopy
from ...json_codec import render_json
from ...context_budget import validate_count
from ...tool.registry import check_descriptions
from .prompt import build_prompt
from .tool import definitions, execute


async def run_retrieval(query, *, pool, search, generate, counter, generation, context_window,
                        tool_template='', max_rounds=16, audit=None):
    tools=definitions()
    for tool in tools:
        check_descriptions(tool['function']['parameters'])
    prefix=[{'role':'system','content':build_prompt(tool_template)},
            {'role':'user','content':'文件查找需求：'+render_json(query)}]
    history,recovery=[],[]
    for _ in range(max_rounds):
        messages=deepcopy(prefix+history+recovery)
        if validate_count(await counter(messages,tools,generation))+generation.max_completion_tokens>context_window:
            raise ValueError('文件检索上下文超限')
        response=await generate(messages,tools,generation)
        valid=(response.finish_reason in ('stop','tool_calls') and not response.assistant_message.get('refusal')
               and len(response.tool_calls)==1)
        if valid:
            call=response.tool_calls[0]
            feedback,ranking,explanation=await execute(pool,search,call.name,call.arguments)
        else:
            feedback={'status':'error','message':'每轮必须恰好提交一个完整且未被拒绝的原生工具调用。'}
        if audit is not None:
            await audit({'assistant_message':deepcopy(response.assistant_message),'feedback':deepcopy(feedback)})
        if feedback['status']=='error':
            if feedback.get('fatal'):
                raise RuntimeError('文件检索依赖不可用')
            recovery.append({'role':'user','content':render_json(feedback)})
            if len(recovery)>=3:
                raise ValueError('文件检索连续纠错达到上限')
            continue
        recovery.clear()
        if feedback.get('finished'):
            return ranking,explanation
        history.extend([{'role':'assistant','content':None,'tool_calls':[{'id':call.call_id,'type':'function',
            'function':{'name':call.name,'arguments':call.arguments}}]},
            {'role':'tool','tool_call_id':call.call_id,'content':render_json(feedback)}])
    raise ValueError('文件检索轮数达到上限')
