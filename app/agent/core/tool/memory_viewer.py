"""按引用查看查询结果，正文可折叠；签名防止资源、页码或会话串用。"""
import hashlib
import hmac
import logging
import secrets
from uuid import uuid4
from pydantic import Field
from ..subgraph.memory_retrieval.schema import RetrievalModel
from .registry import RegisteredTool
from .progress import ToolProgress
from .result import ToolExecution, ToolExecutionResult, FoldableToolContent, ToolPageReference, ToolPageUnavailable


class ViewMemoryQueryArguments(RetrievalModel):
    """当你需要阅读检索命中的历史问题、执行过程和最终答复时，使用这个工具。使用search_memory返回的query_id；省略页码从第一页开始，之后逐页查看。可以指定已读页重看，不重新检索；末页不循环。"""
    query_id: str = Field(strict=True, min_length=1, description='search_memory返回的完整query_id；失效时重新检索，不能自行构造或使用其他任务的查询标识。')
    page: int | None = Field(default=None, strict=True, ge=1, description='从1开始的页码；省略或null查看下一页，首次为第一页。末页不循环，指定已读页可重看。')


class MemoryQueryViewer:
    def __init__(self, pool):
        self.pool = pool
        self._key = secrets.token_bytes(32)

    def _sign(self, resource, locator, nonce):
        return hmac.new(self._key, f'{resource}\n{locator}\n{nonce}'.encode(), hashlib.sha256).hexdigest()

    async def view(self, query_id, page=None):
        try:
            metadata, _, commit = await self.pool.view(query_id, page)
        except Exception as error:
            logging.getLogger(__name__).warning('记忆页面读取失败：error_type=%s',type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={'code':'memory_page_unavailable',
                'message':'查询失效、页码无效或历史任务暂时无法读取；页码未推进，请检查页码或重新检索。'})
        resource, locator, nonce = 'memory-query:'+query_id, f"page:{metadata['page']}", str(uuid4())
        reference = ToolPageReference(resource_id=resource, locator=locator,
            display_id=nonce+'.'+self._sign(resource, locator, nonce), media_type='text',
            description=f"历史记忆，第{metadata['page']}/{metadata['total_pages']}页")
        return ToolExecution(ToolExecutionResult(status='succeeded', content=FoldableToolContent(pages=[reference]),
            tool_result={**metadata,'message':'已读取历史记忆。'}), after_commit=commit)

    async def resolve_page(self, reference):
        try:
            nonce, signature = reference.display_id.split('.',1)
            prefix, number = reference.locator.split(':',1)
            if (reference.media_type != 'text' or not reference.resource_id.startswith('memory-query:')
                    or prefix != 'page' or not number.isdigit() or int(number)<1
                    or not hmac.compare_digest(signature,self._sign(reference.resource_id,reference.locator,nonce))):
                raise ValueError()
        except (ValueError,AttributeError):
            raise ValueError('历史记忆页面引用无效') from None
        try:
            _, text, _ = await self.pool.view(reference.resource_id.removeprefix('memory-query:'),int(number))
        except Exception:
            raise ToolPageUnavailable('历史记忆页面已不可读，需要时请重新检索或查看。') from None
        return [{'type':'text','text':text}]


def build_memory_view_registration(viewer):
    return RegisteredTool('view_memory_query', ViewMemoryQueryArguments.__doc__+f'每页最多{viewer.pool.page_size}条。',
        ViewMemoryQueryArguments, lambda name,raw,workspace,args:workspace,
        executor=lambda args:viewer.view(**args.model_dump()), return_types=('ordinary','foldable'),
        page_resolver=viewer.resolve_page, progress=ToolProgress(type='thinking',message='正在查看历史记忆'))
