"""读取执行时的系统时钟，统一返回北京时间，不改写任务创建时间。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from textwrap import dedent

from pydantic import model_validator

from ..workspace import WorkspaceObject
from ..json_codec import load_model_json
from .progress import ToolProgress
from .hooks import ToolHookContext
from .result import OrdinaryToolContent, ToolExecution, ToolExecutionResult

BEIJING_TIME = timezone(timedelta(hours=8))


class GetCurrentTimeArguments(WorkspaceObject):
    """当你需要确认此刻的日期或时间时，使用这个工具。例如回答“现在几点”，或长任务执行中需要核对最新时间。

    无需参数，以空对象调用。返回调用执行时的北京时间（UTC+08:00）、明确时区及星期；时间包含日期、时分秒和毫秒。

    每次调用重新读取时钟，旧回执只代表当时的时间。结果依赖服务端系统时钟，不证明业务事件发生时间，也不替换历史任务的时间基准。需要日期加减时，将有依据的时间交给 calculate_date。
    """

    @model_validator(mode='before')
    @classmethod
    def ignore_unused_fields(cls, value):
        # 与平台目录工具一致：多余字段没有业务语义，不能影响时钟或伪造返回结果。
        # 原始参数仍留在私有审计，已接受的调用由空模型序列化为 {}。
        return {} if isinstance(value, dict) else value


def parse_current_time_arguments(raw):
    # 先保留公共 JSON 语法、重复键和非有限数检查，再丢弃对象中的多余字段。
    return GetCurrentTimeArguments.model_validate(load_model_json(raw))


async def execute_current_time(arguments: GetCurrentTimeArguments):
    # 显式指定时区，避免容器或宿主机的本地时区影响模型得到的时间。
    current = datetime.now(BEIJING_TIME)
    return ToolExecutionResult(
        status='succeeded',
        tool_result={
            'current_time': current.isoformat(timespec='milliseconds'),
            'timezone': 'UTC+08:00',
            'weekday': ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')[current.weekday()],
        },
    )


def render_current_time_feedback(
    arguments: GetCurrentTimeArguments,
    execution: ToolExecution,
    context: ToolHookContext,
) -> ToolExecution:
    # 只展示执行时已读取的时钟，不在渲染阶段重新取时或改写机器结果。
    if execution.result.status != 'succeeded':
        return execution

    result = execution.result.tool_result
    text = dedent(f"""
        ### 当前时间

        状态：查询成功
        当前时间：{result["current_time"]}
        时区：北京时间（{result["timezone"]}）
        星期：{result["weekday"]}
    """).strip()
    return replace(
        execution,
        result=execution.result.model_copy(
            update={'content': OrdinaryToolContent(text=text)},
        ),
    )


def build_current_time_registration():
    from .registry import RegisteredTool

    def prepare(name, raw, workspace, arguments):
        return workspace

    return RegisteredTool(
        'get_current_time',
        GetCurrentTimeArguments.__doc__,
        GetCurrentTimeArguments,
        prepare,
        parser=parse_current_time_arguments,
        executor=execute_current_time,
        progress=ToolProgress(type='thinking', message='正在获取当前时间'),
        hooks=(render_current_time_feedback,),
    )
