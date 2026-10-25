"""按助理查询引用查看已排序的姓名快照；可折叠正文不写入工具回执。"""
import hashlib
import hmac
import secrets
from uuid import uuid4

from pydantic import Field
from app.agent.core.workspace import WorkspaceObject
from app.agent.core.tool import RegisteredTool, ToolExecution, ToolExecutionResult, FoldableToolContent, ToolPageReference
from app.agent.core.tool.result import ToolPageUnavailable


class ViewAssistantQueryArguments(WorkspaceObject):
    """当你需要查看姓名检索结果、确认消息接收对象时，使用这个工具。展示指定页的 userid 和用户名；确认对象后，将 userid 用作 send_assistant_message 的 recipient_userid。

    使用 search_assistants 返回的查询 ID，并指定从 1 开始的页码。例如先查看第 1 页，仍未找到目标时查看第 2 页，也可以返回已读页。同名用户无法区分时向用户澄清，不随意选择。

    查询失效时重新搜索，页码无效时按返回的总页数修正。目录只帮助确定对象，不表示对方在线或保证消息可送达。
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
        except ValueError:
            return ToolExecutionResult(status='failed', tool_result={
                'code':'assistant_query_unavailable', 'message':'查询已失效或页码无效，请检查页码或重新查询。'})
        resource, locator, nonce = 'assistant-query:'+args.query_id, f'page:{args.page}', str(uuid4())
        reference = ToolPageReference(resource_id=resource, locator=locator,
            display_id=nonce+'.'+self._sign(resource, locator, nonce), media_type='table',
            description=f"助理查询结果，第{args.page}/{metadata['total_pages']}页")
        return ToolExecution(ToolExecutionResult(status='succeeded', tool_result=metadata,
            content=FoldableToolContent(pages=[reference])), after_commit=commit)

    async def resolve_page(self, reference):
        try:
            nonce, signature = reference.display_id.split('.', 1)
            prefix, number = reference.locator.split(':', 1)
            if (reference.media_type != 'table' or not reference.resource_id.startswith('assistant-query:')
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
        return_types=('ordinary','foldable'), page_resolver=viewer.resolve_page)
