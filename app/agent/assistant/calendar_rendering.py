"""日程快照的白名单展示；区分日程状态、邀约响应和实际出席。"""
from datetime import datetime
import unicodedata
from zoneinfo import ZoneInfo

DISPLAY_ZONE = ZoneInfo('Asia/Shanghai')
DESCRIPTION_LIMIT = 6000
ATTENDEE_LIMIT = 100


def _text(value, *, default='未提供', limit=300):
    if not isinstance(value, str) or not value.strip():
        return default
    # 外部单行字段不允许换行或控制符伪造其他日程/状态；描述另用逐行资料块保留段落。
    value = ''.join(f'\\u{ord(c):04x}' if unicodedata.category(c) in ('Cc','Cf','Zl','Zp') else c for c in value)
    return value if limit is None or len(value) <= limit else value[:limit] + '〔字段已截断〕'


def _response(value):
    return {'accepted':'已接受', 'needsAction':'待响应', 'declined':'已拒绝', 'tentative':'暂定'}.get(
        value if isinstance(value, str) else None, '响应状态未知')


def _clock(value):
    return value.astimezone(DISPLAY_ZONE).strftime('%Y-%m-%d %H:%M:%S')


def _event_time(event):
    extra = event.model_extra or {}
    if event.start.date and event.end.date and not event.start.dateTime and not event.end.dateTime:
        # 日期型全天事件按上游边界原样展示，不把结束边界误写成“包含该日”，也不做时区平移。
        return f'全天；开始日期：{_text(event.start.date)}；结束日期边界：{_text(event.end.date)}'
    def moment(value):
        if value.dateTime:
            return _clock(datetime.fromisoformat(value.dateTime.replace('Z','+00:00')))
        return _text(value.date)
    prefix = '全天；起止边界：' if extra.get('isAllDay') is True else ''
    return prefix + moment(event.start) + ' 至 ' + moment(event.end)


def _description(value):
    if not isinstance(value, str) or not value.strip():
        return ['描述：未填写']
    clipped = len(value) > DESCRIPTION_LIMIT
    # 资料块逐行加前缀，保留原文段落；即使正文含标题或角色标记也不变成页面结构。
    lines = ['描述（原文）：'] + ['  │ ' + _text(line, default='', limit=None)
        for line in value[:DESCRIPTION_LIMIT].splitlines()]
    if clipped:
        lines.append(f'〔描述已截断：仅展示前 {DESCRIPTION_LIMIT} 字符，原文共 {len(value)} 字符〕')
    return lines


async def render_calendar_page(items, *, page, total_pages, total, start_rank):
    first = items[0]
    context = first.context
    lines = ['════════════ 日程查询结果 ════════════',
        f'员工：{_text(first.username)}', f'员工 userid：{_text(first.userid, limit=None)}',
        f'查询范围：{_clock(context.start_time)} 至 {_clock(context.end_time)}',
        '显示时区：Asia/Shanghai（UTC+08:00）；全天日期保持钉钉原始日期边界',
        f'查询完成时间：{_clock(context.queried_at)}',
        f'第 {page} / {total_pages} 页 · '+(f'已保留 {total} 条日程（非完整结果）' if context.truncated else f'共 {total} 条日程')]
    if context.truncated:
        lines.append('注意：本日程集已截断；请缩小时间范围重新查询，不能据此判断完整安排或空闲。')
    lines.append('以下为查询时的日历资料；邀约响应不代表实际出席，日程状态不等于官方忙闲状态。')
    for rank, item in enumerate(items, start_rank):
        event = item.event
        data = event.model_extra or {}
        organizer = data.get('organizer') if isinstance(data.get('organizer'), dict) else {}
        location = data.get('location') if isinstance(data.get('location'), dict) else {}
        locations = []
        if isinstance(location.get('displayName'), str) and location['displayName'].strip():
            locations.append(_text(location['displayName'], limit=1000))
        room_names = location.get('meetingRooms')
        if isinstance(room_names, list):
            locations.extend(_text(r) for r in room_names[:100] if isinstance(r,str) and r.strip())
        attendees = event.attendees or []
        own = [a for a in attendees if a.get('id') == context.unionid]
        own_status = _response(own[0].get('responseStatus')) if len(own)==1 else '响应状态未知（未返回唯一匹配的员工记录）'
        status = data.get('status')
        event_status = {'confirmed':'已确认', 'cancelled':'已取消', 'tentative':'暂定'}.get(
            status if isinstance(status,str) else None, '状态未知')
        lines.extend(['', f'【{rank}】{_text(event.summary, default="未提供标题")}',
            f'日程 ID：{_text(event.id, limit=None)}', f'时间：{_event_time(event)}',
            '地点：'+('、'.join(dict.fromkeys(locations)) if locations else '未提供'),
            '组织者：'+_text(organizer.get('displayName')), '日程状态：'+event_status,
            '查询员工的邀约响应：'+own_status])
        rooms = data.get('meetingRooms')
        if isinstance(rooms,list) and rooms:
            lines.append('会议室预约状态：')
            for room in rooms[:100]:
                if isinstance(room,dict):
                    lines.append('- '+_text(room.get('displayName'))+'：'+_response(room.get('responseStatus')))
            if len(rooms)>100:
                lines.append('〔会议室列表已截断，仅展示前 100 条〕')
        if isinstance(room_names,list) and len(room_names)>100:
            lines.append('〔地点中的会议室名称已截断，仅展示前 100 条〕')
        lines.extend(['', f'参与人员（已返回 {len(attendees)} 人，完整性未确认）：'])
        if not attendees:
            lines.append('未提供参与人员记录；不能据此判断无人参加。')
        for attendee in attendees[:ATTENDEE_LIMIT]:
            suffix = '〔查询员工〕' if attendee.get('id') == context.unionid else ''
            if attendee.get('isOptional') is True:
                suffix += '〔可选参与〕'
            lines.append('- '+_text(attendee.get('displayName'), default='姓名未提供')+'：'
                +_response(attendee.get('responseStatus'))+suffix)
        if len(attendees)>ATTENDEE_LIMIT:
            lines.append(f'〔参与人员展示已截断：仅展示已返回记录的前 {ATTENDEE_LIMIT} 人〕')
        lines.extend(['', *_description(event.description)])
    lines.extend(['', '════════════ 本页结束 ════════════'])
    return '\n'.join(lines)
