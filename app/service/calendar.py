"""人员目录限定、钉钉分页读取及有界日程快照，不持久化员工日历。"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.model.calendar import CalendarEvent, CalendarReadError

MAX_WINDOW_DAYS = 31


@dataclass(frozen=True)
class CalendarQueryContext:
    start_time: datetime
    end_time: datetime
    queried_at: datetime
    unionid: str
    truncated: bool


@dataclass(frozen=True)
class CalendarQueryItem:
    userid: str
    username: str
    event: CalendarEvent
    context: CalendarQueryContext


@dataclass(frozen=True)
class CalendarSearchResult:
    items: tuple[CalendarQueryItem, ...]
    truncated: bool


class CalendarService:
    def __init__(self, employees, client, *, max_results=1000, max_pages=20, timeout_seconds=60):
        if type(max_results) is not int or max_results < 1 or type(max_pages) is not int or max_pages < 1:
            raise ValueError('日程查询上限必须为正整数')
        self.employees, self.client = employees, client
        self.max_results, self.max_pages, self.timeout_seconds = max_results, max_pages, timeout_seconds

    async def search(self, userid, start_time, end_time):
        if (start_time.utcoffset() is None or end_time.utcoffset() is None
                or not timedelta(0) < end_time.astimezone(timezone.utc) - start_time.astimezone(timezone.utc) <= timedelta(days=MAX_WINDOW_DAYS)):
            raise CalendarReadError('calendar_range_invalid',
                '时间范围必须带时区、结束晚于开始，且不超过 31 天；本次尚未查询。请按实际需求修正或拆分范围。')
        try:
            async with asyncio.timeout(self.timeout_seconds):
                employee = await self.employees.find_by_userid(userid)
                if employee is None:
                    raise CalendarReadError('calendar_employee_not_found',
                        '本项目人员目录中未找到该员工，尚未请求钉钉日程。请使用 search_assistants 和 view_assistant_query 核对准确 userid，'
                        '不要传姓名、unionId 或猜测的标识。')
                if employee.userid != userid:
                    raise CalendarReadError('calendar_employee_mismatch',
                        '人员目录返回的身份与请求不一致，尚未查询钉钉日程。请管理员核对员工资料后重试，不要使用返回的其他身份继续查询。')
                unionid = await self.client.get_unionid(employee.userid)
                events, cursors, cursor = {}, set(), None
                for _ in range(self.max_pages):
                    page = await self.client.events_view(unionid, start_time=start_time.isoformat(),
                        end_time=end_time.isoformat(), next_token=cursor)
                    for event in page.events:
                        # 循环实例按事件标识和发生时间区分；重复分页只去重完全相同的对象。
                        key = (event.id, event.start.dateTime, event.start.date)
                        if key in events and events[key] != event:
                            raise CalendarReadError('calendar_changed_during_query',
                                '分页期间同一日程出现不一致内容，未建立日程集。请重新查询以取得一致结果，不能把此次部分结果当作完整安排。')
                        events[key] = event
                    if page.nextToken and page.nextToken in cursors:
                        raise CalendarReadError('calendar_pagination_invalid',
                            '钉钉返回了重复的分页游标，未建立日程集。请缩小时间范围后重试；持续失败时请管理员检查接口分页。')
                    truncated = len(events) > self.max_results or (len(events) >= self.max_results and bool(page.nextToken))
                    if truncated or not page.nextToken:
                        break
                    cursor = page.nextToken
                    cursors.add(cursor)
                else:
                    # 达到安全页数上限时只发布明确标注截断的有效资料，不假装已读完。
                    truncated = True
                # 每个结果集共享不可变查询元数据；翻页不依赖模型记住查询条件，也不重新读取实时日历。
                context = CalendarQueryContext(start_time, end_time, datetime.now(timezone.utc), unionid, truncated)
                items = tuple(CalendarQueryItem(employee.userid, employee.name or employee.userid, e.model_copy(deep=True), context)
                    for e in list(events.values())[:self.max_results])
                return CalendarSearchResult(items, truncated)
        except TimeoutError:
            raise CalendarReadError('calendar_timeout',
                '日程查询超时，未建立日程集，已读取的部分数据未发布。请缩小时间范围或稍后重试，不能据此判断员工没有安排。') from None
