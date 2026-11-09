"""沿用参考项目六工具多轮规划；有限纠错，成功后清除失败轨迹。"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from textwrap import dedent
from .schema import MemoryRetrievalRequest, MemoryRetrievalResult
from .session import MemoryRetrievalSession
from .tool import build_memory_retrieval_registry
from .prompt import build_memory_retrieval_prompt
from ...json_codec import render_json, ModelActionRejected
from ...workspace import empty_workspace
from ...context_budget import validate_count
from ...tool.registry import check_descriptions


def failure(code, message):
    return MemoryRetrievalResult(status='error', error_code=code, error=message)


async def run_memory_retrieval(request, *, search, generate, counter, generation, context_window,
                               tool_template='', max_rounds=16, max_errors=3):
    request = MemoryRetrievalRequest.model_validate(request)
    session = MemoryRetrievalSession(request, search=search)
    stamp = datetime.fromtimestamp(request.reference_time/1000, timezone(timedelta(hours=8))).isoformat()
    prefix = [{'role':'system','content':build_memory_retrieval_prompt(tool_template)},
        {'role':'user','content':'参考时间（北京时间）：'+stamp+'\n检索需求：'+render_json(request.query)+
            '\n初始查询条件：'+render_json(session.conditions.model_dump())}]
    registry = build_memory_retrieval_registry(session)
    tools = registry.definitions()
    for tool in tools:
        check_descriptions(tool['function']['parameters'])
    history, recovery = [], []
    for _ in range(max_rounds):
        messages = deepcopy(prefix+history+recovery)
        if validate_count(await counter(messages, tools, generation))+generation.max_completion_tokens > context_window:
            return failure('context_limit','记忆查询条件超过模型可用上下文，请缩短检索需求。')
        try:
            # generate 由宿主保存每次原生响应与失败分片，不能把私有推理写进查询历史。
            response = await generate(messages, tools, generation)
        except Exception:
            return failure('model_unavailable','记忆检索模型暂时不可用，请稍后重试。')
        valid = (response.finish_reason in ('stop','tool_calls') and not response.assistant_message.get('refusal')
            and len(response.tool_calls) == 1)
        try:
            if not valid:
                raise ModelActionRejected(
                    '本轮未提交唯一完整且未被拒绝的工具调用，查询条件未修改。',
                    '请提交一个完整的检索工具调用；不从普通文字或未完成输出推断查询结果。',
                )
            call = response.tool_calls[0]
            arguments, _ = registry.prepare(call.name, call.arguments, empty_workspace())
        except ModelActionRejected as error:
            text = dedent("""
                ### 记忆检索调用校验

                状态：调用未执行
                原因：{reason}
                下一步：{action}
            """).strip().format(reason=error.reason, action=error.required_action)
            recovery.append({'role': 'user', 'content': text})
            if len(recovery) >= max_errors:
                return failure('correction_limit', '记忆检索连续纠错达到上限，本次查询未完成。请缩短需求并明确检索对象后重试。')
            continue
        execution = await registry.execute(call.name, arguments)
        feedback = execution.result.tool_result
        if feedback['status'] == 'error':
            if feedback.get('error_code') in ('query_execution_failed','query_busy'):
                return failure(feedback['error_code'], feedback['error'])
            recovery.append({'role': 'user', 'content': execution.result.content.text})
            if len(recovery) >= max_errors:
                return failure('correction_limit','记忆检索连续纠错达到上限，本次查询未完成。请缩短需求并明确检索对象后重试。')
            continue
        recovery.clear()
        if call.name == 'execute_query':
            return session.result
        history.extend([{'role':'assistant','content':None,'tool_calls':[{
            'id':call.call_id,'type':'function','function':{'name':call.name,'arguments':arguments.model_dump_json()}}]},
            {'role':'tool','tool_call_id':call.call_id,'content':execution.result.content.text}])
    return failure('round_limit','记忆检索达到轮数上限，本次查询未完成。请缩小检索范围后重试。')
