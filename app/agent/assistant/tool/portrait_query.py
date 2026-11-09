"""姓名全文检索与画像分页；查询快照在动作提交后发布，页面正文可折叠。"""
import hashlib
import hmac
import logging
from textwrap import dedent
from app.agent.core.tool.feedback import with_feedback
import secrets
from uuid import uuid4

from pydantic import TypeAdapter

from app.agent.core.query_pool import QueryResultPool
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult, FoldableToolContent, ToolPageReference
from app.agent.core.tool.result import ToolPageUnavailable
from app.agent.core.query_pool import QueryViewRejected
from app.model.assistant_portrait import PortraitGroup, PortraitSubject
from .portrait_contract import (PORTRAIT_TOOL_CONTRACTS, SearchPortraitsArguments, SearchPortraitsResult,
    ViewPortraitQueryArguments, ViewPortraitQueryResult, render_portrait_groups)

logger = logging.getLogger(__name__)


def build_portrait_query_pool(owner_userid: str, *, capacity=10, page_size=10):
    owner = PortraitSubject(userid=owner_userid)

    async def render(rows, *, page, total_pages, **metadata):
        # query_id 已在工具回执中展示，正文只负责用户分组与偏好，不重复传递引用。
        return render_portrait_groups(rows, owner_userid=owner.userid, page=page, total_pages=total_pages)

    return QueryResultPool(render, capacity=capacity, page_size=page_size)


def build_search_portraits_registration(*, pool: QueryResultPool, repository, owner_userid: str, max_results=1000):
    owner = PortraitSubject(userid=owner_userid)
    if type(max_results) is not int or max_results < 1:
        raise ValueError('画像查询数量必须为正整数')

    async def execute(args):
        try:
            if pool.closed:
                raise ValueError('画像查询已结束')
            rows = TypeAdapter(list[PortraitGroup]).validate_python(await repository.search(
                owner.userid, args.name, limit=max_results + 1))
            if pool.closed or len(rows) > max_results + 1 or len({r.subject.userid for r in rows}) != len(rows):
                raise ValueError('画像查询返回的对象不合法或已关闭')
            keys = [p.key for group in rows for p in group.portraits]
            if len(set(keys)) != len(keys):
                raise ValueError('画像查询包含重复 key')
            if not rows:
                result = SearchPortraitsResult(query_id=None, total_subjects=0, total_portraits=0, total_pages=0)
                return ToolExecutionResult(status='succeeded', tool_result={**result.model_dump(),
                    'truncated': False, 'message': '没有匹配画像；不代表员工不存在。'})
            truncated = len(rows) > max_results
            rows = rows[:max_results]
            query_id, commit = pool.prepare(rows)
            result = SearchPortraitsResult(query_id=query_id, total_subjects=len(rows),
                total_portraits=sum(len(r.portraits) for r in rows),
                total_pages=(len(rows) + pool.page_size - 1) // pool.page_size)
        except Exception as error:
            logger.warning('画像查询失败：error_type=%s', type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={
                'code': 'portrait_search_unavailable', 'message': '画像查询暂不可用，请稍后重试；不能据此判断没有画像。'})
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result={**result.model_dump(),
            'truncated': truncated, 'message': '请用 view_portrait_query 查看。' +
                ('结果已截断，计数仅代表保留的对象，请缩小名称范围。' if truncated else '')}), after_commit=commit)

    return RegisteredTool('search_portraits', PORTRAIT_TOOL_CONTRACTS[0].description +
        '\n支持姓名全文检索，完整姓名优先；名称过多时缩小范围。无匹配不生成结果集，查询失败不表示没有画像。',
        SearchPortraitsArguments, lambda name, raw, workspace, arguments: workspace, executor=execute, hooks=(render_portrait_search_feedback,))


def render_portrait_search_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 画像查找

            状态：查询失败
            原因及处理方向：{message}
        """).strip().format(**result)
    else:
        text = dedent("""
            ### 画像查找

            状态：查询完成
            查询 ID：{query}
            匹配用户：{total_subjects} 位
            偏好记录：{total_portraits} 条
            总页数：{total_pages}
            结果范围：{scope}
            结果及下一步：{message}
        """).strip().format(**result, query=result['query_id'] or '未生成，无需翻页',
            scope='仅保留部分匹配用户' if result['truncated'] else '全部匹配用户')
    return with_feedback(execution, text)

def render_portrait_view_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 画像查看

            状态：查看失败
            原因及处理方向：{message}
        """).strip().format(**result)
        return with_feedback(execution, text)
    template = dedent("""
        ### 画像查看

        状态：查看记录已提交
        查询 ID：{query_id}
        当前页：第 {page} / {total_pages} 页
        结果集用户：{total_subjects} 位
        资料状态：{state}
        下一步：{action}
    """).strip()
    return with_feedback(
        execution,
        template.format(**result, state='本页正文见随后提供的资料。',
                        action='需要时指定有效页码继续查看或重看。' if result['has_next'] else '已到末页，可指定页码重看。'),
        hidden=template.format(**result, state='本页正文当前未展示，不能从查看记录推断原文。',
                               action='需要核对时，使用此查询 ID 和页码重新查看。'),
        unavailable=template.format(**result, state='本页资料当前无法读取，不能推断原文。',
                                    action='请调用 search_portraits 重新查询，再查看新结果。'),
    )

class PortraitQueryViewer:
    def __init__(self, pool: QueryResultPool):
        self.pool = pool
        self._secret = secrets.token_bytes(32)

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._secret, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    async def view(self, args):
        try:
            metadata, _, commit = await self.pool.view(args.query_id, args.page)
        except QueryViewRejected as error:
            return ToolExecutionResult(status='failed', tool_result={
                'code': 'portrait_query_unavailable', 'message': error.reason + '\n' + error.required_action})
        except Exception as error:
            logger.warning('画像页面读取失败：error_type=%s', type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={
                'code':'portrait_page_unavailable',
                'message':'画像页面暂不可用，本次未读取页面，页码未推进；请稍后重试查看，不能据此判断没有画像。'})
        metadata['total_subjects'] = metadata.pop('total')
        metadata = ViewPortraitQueryResult.model_validate(metadata).model_dump()
        resource, locator, nonce = 'portrait-query:' + args.query_id, f'page:{args.page}', uuid4().hex
        reference = ToolPageReference(resource_id=resource, locator=locator,
            display_id=nonce + '.' + self._sign(resource, locator, nonce), media_type='text',
            description=f"用户画像，第{args.page}/{metadata['total_pages']}页")
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result=metadata,
            content=FoldableToolContent(pages=[reference])), after_commit=commit)

    async def resolve_page(self, reference):
        try:
            nonce, signature = reference.display_id.split('.', 1)
            prefix, number = reference.locator.split(':', 1)
            if (reference.media_type != 'text' or not reference.resource_id.startswith('portrait-query:')
                    or prefix != 'page' or not number.isdigit() or int(number) < 1
                    or not hmac.compare_digest(signature, self._sign(reference.resource_id, reference.locator, nonce))):
                raise ValueError('无效画像页面')
            _, text, _ = await self.pool.view(reference.resource_id.removeprefix('portrait-query:'), int(number))
        except Exception:
            raise ToolPageUnavailable('画像页面已失效，需要时请重新查询或查看。') from None
        return [{'type': 'text', 'text': text}]


def build_view_portrait_query_registration(viewer: PortraitQueryViewer):
    return RegisteredTool('view_portrait_query', PORTRAIT_TOOL_CONTRACTS[1].description +
        f'\n每页最多 {viewer.pool.page_size} 位用户，展示其全部偏好；数字 key 是稳定标识。',
        ViewPortraitQueryArguments, lambda name, raw, workspace, arguments: workspace,
        executor=viewer.view, return_types=('ordinary', 'foldable'), page_resolver=viewer.resolve_page, hooks=(render_portrait_view_feedback,))
