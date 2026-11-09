"""增量规划 → 并行生成 → 全量验收；任一失败不产生部分摘要。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from textwrap import dedent
from pydantic import ValidationError
from app.infrastructure.tool_tag import tool_tag_section
from ....context_budget import validate_count
from ....json_codec import (load_model_json, validate_model_payload, validate_model_json,
    ModelActionRejected, model_validation_feedback)
from .schema import (SummaryTopic, FinishPlanningArguments, TopicGenerationOutput,
    TopicSummary, TopicMemory, planning_tools, generation_schema)
from .prompt import build_topic_planning_prompt, TOPIC_GENERATION_PROMPT
from .rendering import prepare_sources, render_material, render_topic
from .sampling import summary_generation
from ....tool.registry import RegisteredTool, ToolRegistry
from ....tool.result import OrdinaryToolContent, ToolExecution


def render_topic_selection_feedback(
    arguments,
    execution: ToolExecution,
    context,
) -> ToolExecution:
    # 主题名称等资料先以既有 YAML 边界展示；回执不重复资料或声称摘要已经生成。
    text = dedent("""
        ### 主题选择

        状态：主题选择已接受，最新主题集合见本轮提供的资料。
        下一步：继续选择或更新主题；筛选完成后调用 finish_topic_planning。
    """).strip()
    return replace(
        execution,
        result=execution.result.model_copy(
            update={'content': OrdinaryToolContent(text=text)},
        ),
    )


async def checked_generate(messages, tools, generation, *, generate, counter, context_window, **kwargs):
    count = validate_count(await counter(messages, tools, generation))
    if count + generation.max_completion_tokens > context_window:
        raise ValueError('主题整理输入与输出预留超过上下文上限')
    return await generate(messages, tools, generation, **kwargs)


def validate_topic(topic, tasks, previous):
    if not set(topic.task_ids) <= tasks.keys() or not set(topic.previous_topic_ids) <= previous.keys():
        raise ModelActionRejected('主题引用了本次未提供的来源。', 'task_ids 和 previous_topic_ids 只能选择本次材料中的实际 ID，不自拟或借用其他主题来源。')


async def plan_topics(tasks, previous, *, focus, generate, counter, generation, context_window,
                      max_errors, tool_call_template):
    tools = planning_tools()
    selection = next(tool['function'] for tool in tools if tool['function']['name'] == 'extract_topic')
    feedback_registry = ToolRegistry([
        RegisteredTool(
            'extract_topic',
            selection['description'],
            SummaryTopic,
            lambda name, raw, workspace, arguments: workspace,
            hooks=(render_topic_selection_feedback,),
        ),
    ])
    base = [{'role': 'system', 'content': build_topic_planning_prompt(tool_call_template)},
        {'role': 'user', 'content': render_material(tasks, previous)+'\n\n整理重点：'+focus}]
    topics = {}
    temporary = []
    errors = 0
    accepted_feedback = []
    for _ in range(96):
        # 权威主题集合每轮重建；合法动作后清除整段错误调用及反馈。
        from ....context_rendering.block import metadata_block
        state = {'role': 'user', 'content': '已接受的主题选择：\n'+metadata_block(
            [t.model_dump() for t in topics.values()])}
        response = await checked_generate(base+accepted_feedback+[state]+temporary, tools, generation,
            generate=generate, counter=counter, context_window=context_window)
        argument_model=SummaryTopic
        try:
            if response.assistant_message.get('refusal') or response.finish_reason not in ('stop', 'tool_calls'):
                raise ModelActionRejected('主题规划响应被拒绝或未完整生成，主题集合未修改。', '在允许范围内提交一个完整规划工具调用；不从未完成输出推断规划成功。')
            if not response.tool_calls and response.finish_reason == 'stop' and any(
                    isinstance(response.assistant_message.get(key), str) and response.assistant_message[key].strip()
                    for key in ('content', 'reasoning', 'reasoning_content')):
                # 分析文字仅暂存，不变更主题集合；原生推理始终只进入私有生成审计。
                if response.content and response.content.strip():
                    temporary.append({'role': 'assistant', 'content': response.content})
                temporary.append({'role': 'user', 'content': '主题集合未修改，规划尚未完成；准备好后用extract_topic提交主题，最终用finish_topic_planning结束。'})
                continue
            if len(response.tool_calls) != 1:
                raise ModelActionRejected('本轮未提交唯一的主题规划工具调用，主题集合未修改。', '每次只调用一个 extract_topic 或 finish_topic_planning，等待结果后再继续。')
            call = response.tool_calls[0]
            if call.name == 'extract_topic':
                topic = validate_model_payload(SummaryTopic, load_model_json(call.arguments))
                validate_topic(topic, tasks, previous)
                if topic.topic_id not in topics and len(topics) >= 32:
                    raise ModelActionRejected('已有32个主题，不能继续新增。', '合并重叠主题，或使用已有 topic_id 更新主题；规划足够时调用 finish_topic_planning。')
                execution = await feedback_registry.execute('extract_topic', topic)
                topics[topic.topic_id] = topic
                # 仅保留最近成功回执，权威主题集合每轮重建，不累积已被替换的旧选择。
                accepted_feedback = [
                    {
                        'role': 'assistant',
                        'content': None,
                        'tool_calls': [{
                            'id': call.call_id,
                            'type': 'function',
                            'function': {'name': call.name, 'arguments': topic.model_dump_json()},
                        }],
                    },
                    {'role': 'tool', 'tool_call_id': call.call_id, 'content': execution.result.content.text},
                ]
                errors = 0
                temporary.clear()
            elif call.name == 'finish_topic_planning':
                argument_model=FinishPlanningArguments
                validate_model_payload(FinishPlanningArguments, load_model_json(call.arguments))
                return list(topics.values())
            else:
                raise ModelActionRejected('请求的工具不属于本次主题规划。', '提交 extract_topic 选择主题，或用 finish_topic_planning 结束规划。')
        except (ValueError, TypeError) as error:
            errors += 1
            if errors >= max_errors:
                raise ValueError('主题规划连续未通过验收') from None
            # 不回显无效参数、异常信息或伪造标识；完整原始响应由调用边界保存。
            if isinstance(error,ValidationError):
                error=model_validation_feedback(error,argument_model)
            reason=error.reason if isinstance(error,ModelActionRejected) else '本次主题规划未通过校验，主题集合未修改。'
            action=error.required_action if isinstance(error,ModelActionRejected) else '按当前规划工具的字段说明选择合法动作，不添加未提供的来源。'
            temporary.append({'role': 'user', 'content': reason+'\n'+action+tool_tag_section(tool_call_template)})
    raise ValueError('主题规划超过最大轮数')


async def generate_topic(topic, tasks, previous, *, generate, counter, generation,
                         context_window, max_errors):
    selected_tasks = {k: v for k, v in tasks.items() if k in topic.task_ids}
    selected_previous = {k: v for k, v in previous.items() if k in topic.previous_topic_ids}
    base = [{'role': 'system', 'content': TOPIC_GENERATION_PROMPT},
        {'role': 'user', 'content': render_material(selected_tasks, selected_previous)+'\n\n'+render_topic(topic)}]
    messages = deepcopy(base)
    for _ in range(max_errors):
        response = await checked_generate(messages, [], generation, generate=generate,
            counter=counter, context_window=context_window, json_schema=generation_schema())
        try:
            if response.tool_calls or response.finish_reason != 'stop' or response.assistant_message.get('refusal'):
                raise ModelActionRejected('本次总结没有提供完整且未被拒绝的 JSON 正文。', '本步骤只输出符合给定 Schema 的 summary JSON，不调用工具，不使用 Markdown 围栏；不绕过拒绝。')
            result = validate_model_json(TopicGenerationOutput, response.content).summary
            for item in result.current_memory + result.open_items:
                if not set(item.task_ids) <= selected_tasks.keys():
                    raise ModelActionRejected('摘要条目的 task_ids 超出本主题提供的来源。', '仅保留本主题材料中实际提供的任务 ID，移除其他引用；依据不足时说明缺口，不添加事实。')
            summary = TopicSummary(topic_id=topic.topic_id, title=topic.title, **result.model_dump())
            return summary
        except (ValueError, TypeError) as error:
            if isinstance(error,ValidationError):
                error=model_validation_feedback(error,TopicGenerationOutput)
            reason=error.reason if isinstance(error,ModelActionRejected) else '摘要结构未通过验收，本次结果未接受。'
            action=error.required_action if isinstance(error,ModelActionRejected) else '只输出给定 Schema 要求的 summary JSON，依据不足时保留未决事项，不添加事实。'
            messages = base+[{'role': 'user', 'content': reason+'\n'+action}]
    raise ValueError('主题总结连续未通过验收')


def reduce_topics(plan, summaries):
    by_id = {s.topic_id: s for s in summaries}
    if len(by_id) != len(summaries) or set(by_id) != {t.topic_id for t in plan}:
        raise ValueError('主题结果缺失、重复或包含未规划主题')
    for topic in plan:
        summary = by_id[topic.topic_id]
        if summary.title != topic.title or any(not set(i.task_ids) <= set(topic.task_ids)
                for i in summary.current_memory + summary.open_items):
            raise ValueError('主题结果与规划不一致')
    return TopicMemory(topics=[by_id[t.topic_id] for t in plan])


async def summarize(*, source, focus, generate, counter, generation, context_window,
                    max_errors, summary_limit, tool_call_template):
    tasks, previous = prepare_sources(source)
    # generation 由宿主传入配置快照；不能传用户本轮请求覆盖后的主任务档位。
    generation = summary_generation(generation)
    common = dict(generate=generate, counter=counter, generation=generation,
        context_window=context_window, max_errors=max_errors)
    plan = await plan_topics(tasks, previous, focus=focus, tool_call_template=tool_call_template, **common)
    pending = [asyncio.create_task(generate_topic(topic, tasks, previous, **common)) for topic in plan]
    try:
        summaries = await asyncio.gather(*pending)
    except BaseException:
        # 一个主题失败或外层取消时，取消并等待其他分支，避免后台继续写审计。
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise
    memory = reduce_topics(plan, summaries)
    from ....context_rendering.summary import render_summary_section
    text = memory.model_dump_json()
    count = validate_count(await counter([{'role': 'user', 'content': render_summary_section(text)}], [], generation))
    if count >= summary_limit:
        raise ValueError('完整主题摘要未达到容量目标')
    return text
