"""个人助理查询指定员工的日程集；通过独立查看工具读取正文。"""
import logging
from textwrap import dedent
from app.agent.core.tool.feedback import with_feedback
from datetime import timedelta, timezone

from pydantic import AwareDatetime, Field, model_validator

from app.agent.core.json_codec import ModelActionRejected, validate_model_json
from app.agent.core.query_pool import QueryResultPool
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult
from app.agent.core.tool.progress import ToolProgress
from app.agent.core.workspace import WorkspaceObject
from app.model.calendar import CalendarReadError
from app.service.calendar import MAX_WINDOW_DAYS
from app.agent.assistant.calendar_rendering import render_calendar_page

logger = logging.getLogger(__name__)


def render_calendar_search_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 日程查找

            状态：查询失败
            原因及处理方向：{message}
        """).strip().format(**result)
    else:
        text = dedent("""
            ### 日程查找

            状态：{state}
            用户 ID：{userid}
            开始时间：{start_time}
            结束时间：{end_time}
            保留日程：{total} 条
            总页数：{total_pages}
            查询 ID：{query}
            结果及下一步：{message}
        """).strip().format(**result,
            state='查询结果不完整，请缩小时间范围' if result['truncated'] else '查询完成',
            query=result.get('query_id') or '未生成，无需翻页')
    return with_feedback(execution, text)

class SearchCalendarEventsArguments(WorkspaceObject):
    """当需要查询指定员工在一段时间内的钉钉主日历安排时，使用此工具。输入本项目员工 userid 和带时区的起止时间，查询包含循环日程展开的日程集；支持本人及人员目录中的其他员工，实际访问仍受钉钉授权限制。

    单次最多 31 天，最多保留 1000 条日程。有结果时返回 query_id、total、total_pages 和 truncated；total 是保留条数，截断时不代表全部数量。结果按钉钉返回顺序保存，每页 10 条，查询集仅在本次任务存续，较早的查询可能被淘汰。返回空结果时不生成 query_id。失败不等于没有日程或员工空闲。

    有结果后用 view_calendar_query 指定 query_id 和页码查看日程正文，不能仅根据数量猜测会议内容、参会人或空闲时段。不要用 view_memory_query、view_assistant_query 读取此查询集。参与人员列表可能不完整，邀约响应不代表实际出席；日程资料不是请假、外出或在岗状态的权威来源。

    示例：查询已知员工明天的安排，将 userid 填为该员工的准确标识，start_time 填为明天 00:00:00+08:00、end_time 填为后天 00:00:00+08:00 的完整 ISO 8601 时间。用户不明确时先查员工目录；相对日期依据当前日期和用户时区换算，不能猜测身份或日期。
    """
    userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'^\S+$',
        description='要查询的本项目员工 userid，可来自可信所属员工信息或员工目录；本人和其他已登记员工均可指定。不要填写姓名、unionId 或猜测的标识，实际可读范围由程序及钉钉授权校验。')
    start_time: AwareDatetime = Field(strict=True,
        description='查询窗口开始时刻，带 UTC 偏移的 ISO 8601，例如 2026-11-02T00:00:00+08:00；与 end_time 合计跨度最多 31 天。')
    end_time: AwareDatetime = Field(strict=True,
        description='查询窗口结束时刻，带 UTC 偏移的 ISO 8601，例如 2026-11-03T00:00:00+08:00；必须晚于 start_time，查询全天时使用次日零点，不使用 23:59:59。')

    @model_validator(mode='after')
    def valid_range(self):
        if not timedelta(0) < self.end_time.astimezone(timezone.utc) - self.start_time.astimezone(timezone.utc) <= timedelta(days=MAX_WINDOW_DAYS):
            raise ModelActionRejected('日程查询结束时间必须晚于开始时间，且单次跨度不能超过 31 天；本次尚未查询。',
                '核对带时区的 start_time 和 end_time；较长范围拆成多个不超过 31 天的窗口，不要扩大或猜测用户要求的时间。')
        return self


def build_calendar_query_pool():
    return QueryResultPool(render_calendar_page, capacity=10, page_size=10)


def build_search_calendar_events_registration(*, pool, service):
    async def execute(args):
        if pool.closed:
            return ToolExecutionResult(status='failed', tool_result={'code': 'calendar_query_expired',
                'message': '本轮日程查询已结束，未建立日程集。请在新的任务中重新查询。'})
        try:
            result = await service.search(args.userid, args.start_time, args.end_time)
            if pool.closed:
                return ToolExecutionResult(status='failed', tool_result={'code': 'calendar_query_expired',
                    'message': '查询期间本轮任务已结束，日程集未发布；请在新的任务中重新查询。'})
            metadata = {'userid': args.userid, 'start_time': args.start_time.isoformat(), 'end_time': args.end_time.isoformat(),
                'total': len(result.items), 'total_pages': (len(result.items) + pool.page_size - 1) // pool.page_size,
                'truncated': result.truncated, 'content_available': bool(result.items)}
            if not result.items:
                return ToolExecutionResult(status='succeeded', tool_result={**metadata,
                    'message': ('本次未保留日程，但查询未完整结束；请缩小时间范围重试，不能据此判断没有日程。'
                        if result.truncated else '该主日历查询窗口内未返回日程，未生成查询 ID；这不代表员工一定空闲、在岗或没有其他安排。')})
            query_id, commit = pool.prepare(result.items)
        except CalendarReadError as error:
            return ToolExecutionResult(status='failed', tool_result={'code': error.code, 'message': error.message})
        except Exception as error:
            logger.warning('日程查询失败：error_type=%s', type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={'code': 'calendar_search_unavailable',
                'message': '日程查询依赖暂不可用，未建立日程集。请稍后重试；不能将查询失败当作没有日程或员工空闲。'})
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result={**metadata, 'query_id': query_id,
            'message': ('日程集仅保留部分结果，请缩小时间范围重新查询。' if result.truncated else '已取得日程集。')
                + '请调用 view_calendar_query，使用本次 query_id 和从 1 开始的页码查看具体内容。'}),
            after_commit=commit)

    return RegisteredTool('search_calendar_events', SearchCalendarEventsArguments.__doc__, SearchCalendarEventsArguments,
        lambda name, raw, workspace, arguments: workspace, executor=execute,
        parser=lambda raw: validate_model_json(SearchCalendarEventsArguments, raw),
        progress=ToolProgress(type='thinking', message='正在查询员工日程'),
        hooks=(render_calendar_search_feedback,))
