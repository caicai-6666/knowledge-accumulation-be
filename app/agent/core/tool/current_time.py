"""读取执行时的系统时钟，统一返回北京时间，不改写任务创建时间。"""
from datetime import datetime, timedelta, timezone

from ..workspace import WorkspaceObject
from .progress import ToolProgress
from .result import ToolExecutionResult

BEIJING_TIME = timezone(timedelta(hours=8))


class GetCurrentTimeArguments(WorkspaceObject):
    """当你需要确认此刻的日期或时间时，使用这个工具。例如回答“现在几点”，或长任务执行中需要核对最新时间。

    无需参数，以空对象调用。返回调用执行时的北京时间（UTC+08:00）、明确时区及星期；时间包含日期、时分秒和毫秒。

    每次调用重新读取时钟，旧回执只代表当时的时间。结果依赖服务端系统时钟，不证明业务事件发生时间，也不替换历史任务的时间基准。需要日期加减时，将有依据的时间交给 calculate_date。
    """


async def execute_current_time(arguments: GetCurrentTimeArguments):
    # 显式指定时区，避免容器或宿主机的本地时区影响模型得到的时间。
    current = datetime.now(BEIJING_TIME)
    return ToolExecutionResult(status='succeeded', tool_result={
        'current_time': current.isoformat(timespec='milliseconds'),
        'timezone': 'UTC+08:00',
        'weekday': ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')[current.weekday()],
    })


def build_current_time_registration():
    from .registry import RegisteredTool

    def prepare(name, raw, workspace, arguments):
        return workspace

    return RegisteredTool('get_current_time', GetCurrentTimeArguments.__doc__,
        GetCurrentTimeArguments, prepare, executor=execute_current_time,
        progress=ToolProgress(type='thinking', message='正在获取当前时间'))
