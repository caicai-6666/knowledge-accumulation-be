"""按助理查询引用查看已排序的姓名快照；可折叠正文不写入工具回执。"""
import hashlib
import logging
from textwrap import dedent
import hmac
import secrets
from uuid import uuid4

from pydantic import Field
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult, FoldableToolContent, ToolPageReference
from app.agent.core.tool.result import ToolPageUnavailable
from app.agent.core.query_pool import QueryViewRejected
from app.agent.core.tool.feedback import with_feedback


def render_assistant_query_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status != 'succeeded':
        text = dedent("""
            ### 助理目录查看

            状态：查看失败
            原因：{message}
            下一步：{required_action}
        """).strip().format(**result)
        return with_feedback(execution, text)
    template = dedent("""
        ### 助理目录查看

        状态：查看记录已提交
        查询 ID：{query_id}
        当前页：第 {page} / {total_pages} 页
        结果集用户：{total} 位
        资料状态：{state}
        下一步：{action}
    """).strip()
    return with_feedback(
        execution,
        template.format(**result, state='本页目录见随后提供的资料。',
                        action='确认目标后可使用其 userid 发送助理消息；需要时指定页码继续查看或重看。'),
        hidden=template.format(**result, state='本页目录当前未展示，不能从查看记录推断人员信息。',
                               action='需要核对时，使用此查询 ID 和页码重新查看。'),
        unavailable=template.format(**result, state='本页目录当前无法读取，不能推断人员信息。',
                                    action='请重新调用 search_assistants，再查看新结果。'),
    )


class ViewAssistantQueryArguments(WorkspaceObject):
    """当你需要查看助理目录或姓名、部门检索结果、确认消息接收对象时，使用这个工具。按用户分组展示指定页的 userid、用户名、在职状态及全部所属一级部门；确认对象后，将 userid 用作 send_assistant_message 的 recipient_userid。

    使用 search_assistants 返回的查询 ID，并指定从 1 开始的页码。例如先查看第 1 页，仍未找到目标时查看第 2 页，也可以返回已读页。同名用户无法区分时向用户澄清，不随意选择。

    多部门是并列归属，不代表主次、职位、主管或权限；未知部门不推断。可结合部门辨认同名对象，仍不明确时先澄清。在职状态展示查询时的员工记录，在职和离职员工均可被查到；需要最新状态时重新搜索。查询失效时重新搜索，页码无效时按返回的总页数修正。目录只帮助确定对象，不表示对方在线或保证消息可送达。
    """
    query_id: str = Field(strict=True, min_length=1, max_length=128, description='本次任务中 search_assistants 返回的完整 query_id，必须原样使用；不能缩写、猜测或使用其他查询工具返回的标识。')
    page: int = Field(strict=True, ge=1, description='需要查看的页码，从 1 开始，不能超过查询返回的 total_pages；可以重复查看同一页。')


class AssistantQueryViewer:
    def __init__(self, pool):
        self.pool = pool
        self._key = secrets.token_bytes(32)

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    async def view(self, args):
        try:
            metadata, _, commit = await self.pool.view(args.query_id, args.page)
        except QueryViewRejected as error:
            return ToolExecutionResult(status='failed', tool_result={
                'code':'assistant_query_unavailable', 'message':error.reason,
                'required_action':error.required_action})
        except Exception as error:
            logging.getLogger(__name__).warning('助理目录页面读取失败：error_type=%s', type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={
                'code':'assistant_page_unavailable', 'message':'本次目录页面读取失败，页码未推进。',
                'required_action':'请稍后重试查看，不能据此判断目标用户不存在。'})
        resource, locator, nonce = 'assistant-query:'+args.query_id, f'page:{args.page}', str(uuid4())
        reference = ToolPageReference(resource_id=resource, locator=locator,
            display_id=nonce+'.'+self._sign(resource, locator, nonce), media_type='text',
            description=f"助理查询结果，第{args.page}/{metadata['total_pages']}页")
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result=metadata,
            content=FoldableToolContent(pages=[reference])), after_commit=commit)

    async def resolve_page(self, reference):
        try:
            nonce, signature = reference.display_id.split('.', 1)
            prefix, number = reference.locator.split(':', 1)
            if (reference.media_type != 'text' or not reference.resource_id.startswith('assistant-query:')
                    or prefix != 'page' or not number.isdigit() or int(number) < 1
                    or not hmac.compare_digest(signature, self._sign(reference.resource_id, reference.locator, nonce))):
                raise ValueError('无效页面引用')
            _, text, _ = await self.pool.view(reference.resource_id.removeprefix('assistant-query:'), int(number))
        except (ValueError, AttributeError):
            raise ToolPageUnavailable('助理目录已不可读取，需要时请重新查询或查看。') from None
        return [{'type':'text', 'text':text}]


def build_view_assistant_query_registration(viewer):
    return RegisteredTool('view_assistant_query',
        ViewAssistantQueryArguments.__doc__+f'\n每页最多 {viewer.pool.page_size} 条。', ViewAssistantQueryArguments,
        lambda name, raw, workspace, arguments: workspace, executor=viewer.view,
        return_types=('ordinary','foldable'), page_resolver=viewer.resolve_page, hooks=(render_assistant_query_feedback,))
