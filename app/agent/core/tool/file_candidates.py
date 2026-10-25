"""最终文件候选分页与可折叠资料引用；页面读取持续回查权威权限。"""
import hashlib
import hmac
import secrets
from uuid import uuid4
import logging
from ..subgraph.file_retrieval.pool import FileResultError
from ..subgraph.file_retrieval.schema import ViewFileCandidatesArguments
from .registry import RegisteredTool
from .progress import ToolProgress
from .result import ToolExecution,ToolExecutionResult,FoldableToolContent,ToolPageReference,ToolPageUnavailable


class FileCandidateViewer:
    def __init__(self,pool):
        self.pool=pool
        self.key=secrets.token_bytes(32)
        self.issued=set()

    def _sign(self,resource,locator,nonce):
        return hmac.new(self.key,f'{resource}\n{locator}\n{nonce}'.encode(),hashlib.sha256).hexdigest()

    async def view(self,result_id,page=None):
        try:
            metadata,_,advance=await self.pool.view(result_id,page)
        except FileResultError as error:
            return ToolExecutionResult(status='failed',tool_result={'code':'file_page_invalid','message':str(error)})
        except Exception as error:
            logging.getLogger(__name__).warning('文件列表读取失败：error_type=%s',type(error).__name__)
            return ToolExecutionResult(status='failed',tool_result={'code':'file_page_unavailable','message':'文件列表暂不可读，页码未推进。'})
        locator,nonce=f"page:{metadata['page']}",str(uuid4())
        reference=ToolPageReference(resource_id=result_id,locator=locator,
            display_id=nonce+'.'+self._sign(result_id,locator,nonce),media_type='text',
            description=f"文件查询结果，第{metadata['page']}/{metadata['total_pages']}页")
        def commit():
            if not self.pool.closed:
                self.pool.get(result_id)
                advance()
                # 页面内容不缓存；仅允许已提交的精确引用被解析。
                self.issued.add(reference.display_id)
        return ToolExecution(ToolExecutionResult(status='succeeded',content=FoldableToolContent(pages=[reference]),
            tool_result=metadata),after_commit=commit)

    async def resolve_page(self,reference):
        try:
            nonce,signature=reference.display_id.split('.',1)
            prefix,number=reference.locator.split(':',1)
            if (self.pool.closed or reference.display_id not in self.issued or reference.media_type!='text'
                or prefix!='page' or not number.isdigit() or int(number)<1
                or not hmac.compare_digest(signature,self._sign(reference.resource_id,reference.locator,nonce))):
                raise ValueError('无效页面引用')
            _,text,_=await self.pool.view(reference.resource_id,int(number))
            return [{'type':'text','text':text}]
        except Exception:
            raise ToolPageUnavailable('文件列表已不可读，需要时请重新检索或查看。') from None


def build_file_candidates_registration(viewer):
    return RegisteredTool('view_file_candidates',ViewFileCandidatesArguments.__doc__+f'每页最多{viewer.pool.page_size}条。',
        ViewFileCandidatesArguments,lambda name,raw,workspace,args:workspace,
        executor=lambda args:viewer.view(**args.model_dump()),return_types=('ordinary','foldable'),
        page_resolver=viewer.resolve_page,progress=ToolProgress(type='thinking',message='正在查看文件列表'))
