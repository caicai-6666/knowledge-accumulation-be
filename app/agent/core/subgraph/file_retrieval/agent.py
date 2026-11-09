"""有限多轮查询；失败轨迹只参与连续纠错，原生生成及反馈另存私有审计。"""
from copy import deepcopy
from textwrap import dedent
from ...json_codec import render_json, ModelActionRejected
from ...workspace import empty_workspace
from ...context_budget import validate_count
from ...tool.registry import check_descriptions
from .prompt import build_prompt
from .tool import FileRetrievalToolSession


async def run_retrieval(query, *, pool, search, generate, counter, generation, context_window,
                        tool_template='', max_rounds=16, audit=None):
    session = FileRetrievalToolSession(pool, search)
    registry = session.registry
    tools = registry.definitions()
    for tool in tools:
        check_descriptions(tool['function']['parameters'])
    prefix=[{'role':'system','content':build_prompt(tool_template)},
            {'role':'user','content':'文件查找需求：'+render_json(query)}]
    history,recovery=[],[]
    for _ in range(max_rounds):
        messages=deepcopy(prefix+history+recovery)
        if validate_count(await counter(messages,tools,generation))+generation.max_completion_tokens>context_window:
            raise ModelActionRejected('文件检索上下文超过容量限制，本次查询未完成。', '请缩短查找需求或缩小范围后重新检索。')
        response=await generate(messages,tools,generation)
        valid=(response.finish_reason in ('stop','tool_calls') and not response.assistant_message.get('refusal')
               and len(response.tool_calls)==1)
        try:
            if not valid:
                raise ModelActionRejected('本轮未提交唯一完整且未被拒绝的工具调用，未执行操作。',
                    '每轮提交一个本次提供的文件检索工具调用，不从普通文字或未完成输出推断成功。')
            call = response.tool_calls[0]
            arguments, _ = registry.prepare(call.name, call.arguments, empty_workspace())
        except ModelActionRejected as error:
            feedback = {'status': 'error', 'message': error.reason, 'required_action': error.required_action}
            text = dedent("""
                ### 文件检索调用校验

                状态：调用未执行
                原因：{message}
                下一步：{required_action}
            """).strip().format(**feedback)
        else:
            execution = await registry.execute(call.name, arguments)
            feedback = execution.result.tool_result
            text = execution.result.content.text
        if audit is not None:
            await audit({'assistant_message':deepcopy(response.assistant_message),'feedback':deepcopy(feedback)})
        if feedback['status']=='error':
            if feedback.get('fatal'):
                raise ModelActionRejected(feedback['message'], feedback['required_action'])
            recovery.append({'role':'user','content':text})
            if len(recovery)>=3:
                raise ModelActionRejected('文件检索连续纠错达到上限，本次查询未完成。', '请明确文件线索并缩小条件后重新检索，不复用本次内部候选 ID。')
            continue
        recovery.clear()
        if feedback.get('finished'):
            return session.result
        history.extend([{'role':'assistant','content':None,'tool_calls':[{'id':call.call_id,'type':'function',
            'function':{'name':call.name,'arguments':arguments.model_dump_json()}}]},
            {'role':'tool','tool_call_id':call.call_id,'content':text}])
    raise ModelActionRejected('文件检索达到轮数上限，本次查询未完成。', '请缩小检索范围后重新查询。')
