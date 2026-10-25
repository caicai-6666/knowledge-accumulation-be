"""每次任务独立的中途反馈计时；不将提醒写入权威上下文。"""
from time import monotonic

from .prompt.guidance import build_progress_reminder_message, build_task_start_guidence_message


class ProgressReminder:
    def __init__(self, interval_seconds: float, *, clock=monotonic):
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.last_feedback_at = clock()
        self.pending = False

    def message(self):
        if self.interval_seconds <= 0:
            return None
        if not self.pending and self.clock() - self.last_feedback_at < self.interval_seconds:
            return None
        # 未有成功反馈时保持待提醒状态，不能因为刚提醒过就重新等待一个间隔。
        self.pending = True
        return build_progress_reminder_message()

    def acknowledge(self):
        """仅在中途输出已提交、且宿主交付成功后重计；普通工具状态不算反馈。"""
        self.last_feedback_at = self.clock()
        self.pending = False


class ExplorationGuidance:
    """每轮任务创建一份；只在正式模型请求和成功公开交付时调用。"""
    def __init__(self, options):
        self.reminder = ProgressReminder(options.progress_reminder_interval_seconds, clock=monotonic)

    def before_request(self, round_number):
        # 开场与周期建议互斥；后续纠错请求不重复开场。
        return build_task_start_guidence_message() if round_number == 1 else self.reminder.message()

    def after_output(self, kind):
        if kind == 'intermediate':
            self.reminder.acknowledge()
