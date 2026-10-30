"""沿已选祖先链逐任务记账；压缩只取完整前缀，活动任务始终保留。"""
from dataclasses import dataclass
from ..context_rendering.history import render_history_messages
from ..context_rendering.messages import render_current_input
from ..context_rendering.summary import render_summary_section
from ..json_codec import render_json
from ..prompt.guidance import build_system_guidence_message
from .calculator import validate_count


def select_completed_prefix(counts, completed):
    if (type(completed) is not int or completed < 0 or len(counts) != completed + 1
            or any(type(n) is not int or n < 0 for n in counts)):
        raise ValueError('FIFO 计数必须对应完整历史任务及一个活动任务')
    total = sum(counts)
    if not total:
        return 0
    accumulated = 0
    for index, value in enumerate(counts):
        accumulated += value
        if accumulated * 10 >= total * 7:
            return min(index + 1, completed)
    return completed


@dataclass(frozen=True)
class FifoUsage:
    counts: tuple[int, ...]
    budget: int
    prefix_count: int

    @property
    def tokens(self):return sum(self.counts)
    @property
    def warning(self):return self.tokens * 5 >= self.budget * 4
    @property
    def full(self):return self.tokens * 20 >= self.budget * 19


async def measure_fifo(request, context, accepted, *, trajectory_budget, count_text):
    summary_tokens = validate_count(await count_text(render_summary_section(context.summary))) if context.summary else 0
    remaining = trajectory_budget - summary_tokens
    if remaining <= 0:
        raise ValueError('累计摘要耗尽轨迹预算')
    counts = []
    # 取消、失败、调整方向均是封闭任务，保留各自结束语义；只有本轮仍为活动任务。
    for turn in context.turns:
        messages = render_history_messages(context.model_copy(update={'summary':None, 'turns':[turn]}))
        counts.append(validate_count(await count_text(render_json(messages))))
    messages = [{'role':'user', 'content':render_current_input(request)}, *accepted]
    counts.append(validate_count(await count_text(render_json(messages))))
    return FifoUsage(tuple(counts), remaining, select_completed_prefix(counts, len(context.turns)))


def capacity_guidance(usage):
    reason = (f'近期轨迹使用量为{usage.tokens}/{usage.budget} token，已达到95%自动整理阈值。' if usage.full else '近期轨迹使用量已达到80%整理提醒阈值。')
    action = ('下一步将先自动整理完整历史任务，不要重放已经成功的操作。' if usage.full and usage.prefix_count else
              '暂停新的业务探索，优先将待整理历史中仍有效的关键内容保存到工作区。')
    action += (f'本次范围为当前历史中最早的{usage.prefix_count}个完整任务，不包含本轮任务。'
               if usage.prefix_count else '当前没有可整理的完整历史任务；不要反复调用 compact_context，不能删除或摘要化本轮活动轨迹。')
    return {**build_system_guidence_message(kind='context_capacity', reason=reason, required_action=action), 'source':'fifo'}


def compression_notice():
    return {**build_system_guidence_message(kind='context_capacity',
        reason='指定完整历史任务已汇入累计摘要，剩余任务和本轮轨迹保留。',
        required_action='旧整理范围已失效，依据最新摘要、剩余任务和工作区继续；不要重放成功操作。'), 'source':'fifo'}
