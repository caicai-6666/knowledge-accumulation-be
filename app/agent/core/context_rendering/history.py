"""摘要与历史任务的独立消息；只使用已接受轨迹，不从展示正文重建工具调用。"""
from ..model import ContextInjection
from ..native_messages import history_native_messages
from .summary import render_summary_section
from .task import render_task_input,render_task_ending


def render_history(context: ContextInjection):
    """整理子 Agent 使用同一历史来源；其结构化输入不作为主助手整体 user 消息。"""
    return {'summary':context.summary,'questions_and_answers':[render_turn(turn) for turn in context.turns]}


def render_turn(turn):
    result=turn.model_dump(exclude={'status','native_messages'})
    if not result['attachments']:result.pop('attachments')
    if turn.native_messages is not None:
        result['native_messages']=history_native_messages(turn.native_messages,status=turn.status)
    if turn.status!='complete':
        result.pop('answer')
        result['ending']={'cancelled':'用户已终止，不继续执行此请求',
            'superseded':'用户已调整方向，按最新要求处理','failed':'执行失败，未形成最终答案'}[turn.status]
        if turn.status=='cancelled':result['intermediate']=[]
    return result


def render_history_messages(context: ContextInjection):
    messages=[]
    summary=render_summary_section(context.summary)
    if summary:messages.append({'role':'user','content':summary})
    for number,turn in enumerate(context.turns,1):
        messages.append({'role':'user','content':render_task_input(turn.question,task_number=number,attachments=turn.attachments)})
        projected=render_turn(turn)
        native=projected.get('native_messages')
        if native is not None:
            messages.extend(native)
            has_final=any(message['role']=='assistant' and
                message['tool_calls'][0]['function']['name']=='finish_task' for message in native)
            # 无最终工具调用而确有已保存答案时，仅补真实助手正文，不反推参数或回执。
            if turn.status=='complete' and turn.answer and not has_final:
                messages.append({'role':'assistant','content':turn.answer})
        else:
            # 没有原生工具对的显式历史输入，按实际公开输出提供 assistant 消息。
            messages.extend({'role':'assistant','content':text} for text in projected['intermediate'])
            if projected.get('answer'):messages.append({'role':'assistant','content':projected['answer']})
        messages.append({'role':'user','content':render_task_ending(turn.status,task_number=number)})
    return messages
