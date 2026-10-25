"""姓名检索只发布查询引用；具体目录由独立工具分页查看。"""
import logging

from pydantic import Field, TypeAdapter
from app.agent.core.query_pool import QueryResultPool
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult
from app.model.personnel import EmployeeName

logger = logging.getLogger(__name__)


class SearchAssistantsArguments(WorkspaceObject):
    """当你需要按用户名寻找某个人的助理，以便向其求助、协作或发送消息时，使用这个工具。提供姓名关键词，获得查询 ID 和结果数量后，调用 view_assistant_query 查看具体用户；例如先搜索 Alice，再查看查询结果的第一页。

    可以使用完整用户名或姓名片段，结果优先展示完整姓名匹配，再按相关程度排列。结果过多时缩小关键词重新查询；没有匹配用户时不生成查询 ID，无需翻页。查询失败不代表用户不存在。
    """
    keyword: str = Field(strict=True, min_length=1, max_length=200, pattern=r'\S', description='要查找的用户名或姓名关键词，必填，1 至 200 个字符，不能全为空白。例如 Alice、Ali 或“张伟 李明”。英文不区分大小写，关键词不必连续出现；不支持自动纠正错别字，百分号和下划线不是通配符。')


async def render_assistants(rows, **metadata):
    def cell(value):
        return value.replace('\\', '\\\\').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')
    return '\n'.join(['| userid | 用户名 |', '| --- | --- |',
                      *(f'| {cell(row.userid)} | {cell(row.username)} |' for row in rows)])


def build_assistant_query_pool(*, capacity=10, page_size=10):
    # 复用记忆查询的 LRU 实现，但每个任务创建独立实例，ID 与淘汰互不影响。
    return QueryResultPool(render_assistants, capacity=capacity, page_size=page_size)


def build_search_assistants_registration(*, pool, employees, max_results=1000):
    if type(max_results) is not int or max_results < 1:
        raise ValueError('助理查询结果数量必须为正整数')

    async def execute(args):
        if pool.closed:
            return ToolExecutionResult(status='failed', tool_result={
                'code':'assistant_query_expired', 'message':'查询已结束，请重新发起任务。'})
        try:
            # 单条 SQL 取得有序快照；多取一条判断截断，分页不再查询数据库。
            rows = TypeAdapter(list[EmployeeName]).validate_python(await employees.search_user_names(
                args.keyword, limit=max_results + 1))
            if len({row.userid for row in rows}) != len(rows) or len(rows) > max_results + 1:
                raise ValueError('人员目录返回重复身份或超出查询数量')
            if pool.closed:
                raise ValueError('查询已结束')
            if not rows:
                return ToolExecutionResult(status='succeeded', tool_result={
                    'total':0, 'total_pages':0, 'message':'没有匹配用户，未生成查询 ID，无需查看。'})
            truncated = len(rows) > max_results
            rows = rows[:max_results]
            query_id, commit = pool.prepare(rows)
        except Exception as error:
            logger.warning('助理目录查询失败：error_type=%s', type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={
                'code':'assistant_directory_unavailable', 'message':'助理目录暂不可用，不能据此判断没有匹配用户。'})
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result={
            'query_id':query_id, 'total':len(rows),
            'total_pages':(len(rows)+pool.page_size-1)//pool.page_size, 'truncated':truncated,
            'message':'请用 view_assistant_query 查看；结果较多，可缩小关键词重新查询。' if truncated
                      else '请用 view_assistant_query 指定查询 ID 和页码查看。'}), after_commit=commit)

    return RegisteredTool('search_assistants', SearchAssistantsArguments.__doc__, SearchAssistantsArguments,
        lambda name, raw, workspace, arguments: workspace, executor=execute)
