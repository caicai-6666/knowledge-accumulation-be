"""提醒排期的纯计算；UTC 保存，日历规则按显式时区解释。"""
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.model.assistant import OnceSchedule, IntervalSchedule


def next_occurrence(config, zone, after, *, end_at=None):
    """返回严格晚于 after 的时刻；重复的本地时刻仅取第一次，不存在的时刻跳过。"""
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError('比较时间必须带时区')
    after = after.astimezone(timezone.utc)
    tz = ZoneInfo(zone)
    candidate = None
    if isinstance(config, OnceSchedule):
        candidate = config.at if config.at > after else None
    elif isinstance(config, IntervalSchedule):
        step = timedelta(seconds=config.every_seconds)
        cycles = max(0, (after - config.start_at) // step + 1)
        try:
            candidate = config.start_at + cycles * step
        except OverflowError:
            return None
    else:
        day = max(config.start_date, after.astimezone(tz).date())
        clock = datetime.strptime(config.local_time, '%H:%M:%S').time()
        while True:
            if end_at is not None and day > end_at.astimezone(tz).date():
                return None
            matches = config.frequency == 'daily'
            if config.frequency == 'weekly':
                matches = day.isoweekday() in config.weekdays
            if config.frequency == 'monthly':
                last = monthrange(day.year, day.month)[1]
                target = min(config.day_of_month, last) if config.missing_day == 'last_day' else config.day_of_month
                matches = day.day == target
            if matches:
                local = datetime.combine(day, clock)
                options = set()
                for fold in (0, 1):
                    value = local.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
                    # round-trip 排除夏令时跳跃产生的虚构墙上时刻。
                    if value.astimezone(tz).replace(tzinfo=None) == local:
                        options.add(value)
                if options and min(options) > after:
                    candidate = min(options)
                    break
            try:
                day += timedelta(days=1)
            except OverflowError:
                return None
    if candidate is None or (end_at is not None and candidate > end_at):
        return None
    return candidate.astimezone(timezone.utc)


def describe_schedule(config, zone):
    if isinstance(config, OnceSchedule):
        return '一次性：' + config.at.astimezone(ZoneInfo(zone)).isoformat()
    if isinstance(config, IntervalSchedule):
        return f'从 {config.start_at.astimezone(ZoneInfo(zone)).isoformat()} 起，每 {config.every_seconds} 秒'
    if config.frequency == 'daily':
        rule = '每天'
    elif config.frequency == 'weekly':
        rule = '每周' + '、'.join('一二三四五六日'[day - 1] for day in sorted(config.weekdays))
    else:
        rule = f'每月 {config.day_of_month} 日（日期不存在时' + ('取月末' if config.missing_day == 'last_day' else '跳过该月') + '）'
    return f'{rule} {config.local_time}，从 {config.start_date.isoformat()} 起'
