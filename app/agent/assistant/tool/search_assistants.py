"""姓名、部门检索只发布查询引用；具体目录由独立工具分页查看。"""
import logging
from textwrap import dedent
from typing import Annotated

from pydantic import Field, TypeAdapter, field_validator
from app.agent.core.query_pool import QueryResultPool
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult
from app.model.personnel import EmployeeName
from app.agent.assistant.rendering import employee_field, render_departments

from app.agent.core.tool.feedback import with_feedback

logger = logging.getLogger(__name__)


def render_search_assistants_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 助理查找

            状态：查询失败
            原因及处理方向：{message}
        """).strip().format(**result)
    elif not result['total']:
        text = dedent("""
            ### 助理查找

            状态：查询完成，结果为 0 位用户
            结果：{message}
        """).strip().format(**result)
    else:
        text = dedent("""
            ### 助理查找

            状态：查询成功
            查询 ID：{query_id}
            结果数量：{total} 位用户
            总页数：{total_pages}
            结果范围：{scope}
            下一步：{message}
        """).strip().format(**result, scope='仅保留部分匹配用户' if result['truncated'] else '全部匹配用户')
    return with_feedback(execution, text)


class SearchAssistantsArguments(WorkspaceObject):
    """当你需要浏览用户助理目录，或按姓名、所属一级部门寻找助理以便求助、协作或发送消息时，使用这个工具。获得查询 ID 和结果数量后，调用 view_assistant_query 查看具体用户。

    keyword 检索姓名，department 检索一级部门名称，两者均使用全文检索并补充字面包含匹配。任一参数省略、null、空字符串或纯空白时跳过该路；两者都为空时浏览全部用户助理，按用户名和用户标识稳定排序。例如空参数对象浏览全体；keyword="Alice" 只查姓名；department="研发部" 只查部门。

    同时提供两个条件时，取两路结果的并集并按 userid 去重，采用等权排名融合，不直接相加全文分数。例如 keyword="Alice", department="研发部" 会保留姓名匹配或部门匹配的用户，不仅限于研发部的 Alice；若要求同时满足，须查看结果并核对。单路优先完整名称匹配，再按相关程度排序；多部门用户只出现一次，不因命中多个部门重复加分。融合排名不是相似度百分比，不保证错别字或同音匹配。

    查看结果时可见用户名、userid、在职状态和全部所属一级部门，用于辨认同名用户。在职和离职员工均可被查到，在职状态不表示助理在线或消息可送达。部门并列归属，无主次，也不表示额外权限。

    结果超过数量上限时返回 truncated=true，此时只保留部分结果，可提供更具体的姓名或部门关键词重新查询；不能把截断结果当作完整目录。空结果不生成查询 ID，无需翻页。任一路查询失败时整次查询失败，不把另一部分当作完整结果，也不代表用户不存在。
    """
    keyword: Annotated[str, Field(strict=True, max_length=200)] | None = Field(
        default=None, description='可选的用户名或姓名关键词，最多200字符；省略或 null、空串、纯空白跳过姓名检索，两参数均为空才浏览全体。仅检索姓名，例如 Alice、Ali 或“张伟 李明”；英文不区分大小写，不自动纠错，百分号和下划线按字面匹配。与 department 同时提供时采用并集排名融合，不是交集过滤。')
    department: Annotated[str, Field(strict=True, max_length=200)] | None = Field(
        default=None, description='可选的所属一级部门名称关键词，最多200字符，例如“研发部”或“产品”；全文检索并补充字面包含，不传部门ID。省略、null、空串、纯空白跳过部门检索；匹配任一所属一级部门即可召回该用户，结果展示全部所属部门。与 keyword 同时提供时取并集并融合排名，不表示只在该部门内查姓名。')

    @field_validator('keyword', 'department')
    @classmethod
    def normalize_query(cls, value):
        return (value.strip() or None) if value is not None else None


async def render_assistants(rows, **metadata):
    template = dedent("""
        【{username}的助理】
        用户ID：{userid}
        在职状态：{employment_status}
        {departments}
    """).strip()
    groups = [template.format(
        username=employee_field(row.username), userid=employee_field(row.userid),
        employment_status={'employed': '在职', 'resigned': '离职'}[row.employment_status],
        departments=render_departments(row.departments),
    ) for row in rows]
    return '\n\n'.join(['════════════ 助理目录 ════════════', *groups,
                         '════════════ 本页结束 ════════════'])


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
                args.keyword, department=args.department, limit=max_results + 1))
            if len({row.userid for row in rows}) != len(rows) or len(rows) > max_results + 1:
                raise ValueError('人员目录返回重复身份或超出查询数量')
            if pool.closed:
                raise ValueError('查询已结束')
            if not rows:
                return ToolExecutionResult(status='succeeded', tool_result={
                    'total':0, 'total_pages':0, 'message':('人员目录为空，未生成查询 ID，无需查看。' if args.keyword is None and args.department is None
                        else '没有匹配用户，未生成查询 ID，无需查看。')})
            truncated = len(rows) > max_results
            rows = rows[:max_results]
            query_id, commit = pool.prepare(rows)
        except Exception as error:
            logger.warning('助理目录查询失败：error_type=%s', type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={
                'code':'assistant_directory_unavailable', 'message':'助理目录暂不可用，本次未生成查询结果；请稍后重新查询，不能据此判断没有匹配用户。'})
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result={
            'query_id':query_id, 'total':len(rows),
            'total_pages':(len(rows)+pool.page_size-1)//pool.page_size, 'truncated':truncated,
            'message':'请用 view_assistant_query 查看；结果较多，仅返回部分用户，请提供更具体的姓名或部门关键词重新查询。' if truncated
                      else '请用 view_assistant_query 指定查询 ID 和页码查看。'}), after_commit=commit)

    return RegisteredTool('search_assistants', SearchAssistantsArguments.__doc__, SearchAssistantsArguments,
        lambda name, raw, workspace, arguments: workspace, executor=execute, hooks=(render_search_assistants_feedback,))
