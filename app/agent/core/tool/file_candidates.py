"""最终文件候选分页与可折叠资料引用；页面读取持续回查权威权限。"""
import hashlib
import hmac
import secrets
from uuid import uuid4
import logging
from dataclasses import replace
from textwrap import dedent
from ..subgraph.file_retrieval.pool import FileResultError
from ..subgraph.file_retrieval.schema import ViewFileCandidatesArguments
from .registry import RegisteredTool
from .progress import ToolProgress
from .result import (
    ToolExecution, ToolExecutionResult, FoldableToolContent, FoldableReceiptText,
    OrdinaryToolContent, ToolPageReference, ToolPageUnavailable,
)


def render_file_candidates_feedback(arguments, execution, context):
    result = execution.result.tool_result
    if execution.result.status == 'failed':
        text = dedent("""
            ### 文件候选查看

            状态：查看失败，本次未返回页面，页码未推进。
            原因：{message}
            下一步：{required_action}
        """).strip().format(**result)
        content = OrdinaryToolContent(text=text)
    else:
        template = dedent("""
            ### 文件候选查看

            状态：查看记录已提交
            结果集 ID：{result_id}
            当前页：第 {page} / {total_pages} 页
            结果集候选：{count} 个
            资料状态：{display_state}
            下一步：{next_step}
        """).strip()
        next_step = (
            '可省略 page 查看下一页，或指定有效页码重看。'
            if result['has_next'] else '已到末页；需要重看时请明确指定有效页码。'
        )
        feedback = FoldableReceiptText(
            visible=template.format(
                **result,
                display_state='本页候选见随后提供的文件资料，候选命中不代表原文已核实。',
                next_step=next_step,
            ),
            hidden=template.format(
                **result,
                display_state='本页候选当前未展示，不能从查看记录推断文件信息。',
                next_step='需要核对时使用此结果集 ID 和页码重新查看；引用失效则重新检索。',
            ),
            unavailable=template.format(
                **result,
                display_state='本页文件资料当前无法读取，不能据此推断文件信息。',
                next_step='请重新检索文件并查看新结果。',
            ),
        )
        content = execution.result.content.model_copy(update={'feedback': feedback})
    return replace(
        execution,
        result=execution.result.model_copy(update={'content': content}),
    )


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
            # 只消费就近校验提供的定向反馈，不直接回显任意异常字符串。
            return ToolExecutionResult(status='failed', tool_result={
                'code': 'file_page_invalid',
                'message': error.reason if error.required_action else '文件列表未通过读取校验。',
                'required_action': error.required_action or '请重新检索并查看新结果，不使用未通过校验的文件资料。',
            })
        except Exception as error:
            logging.getLogger(__name__).warning('文件列表读取失败：error_type=%s',type(error).__name__)
            return ToolExecutionResult(status='failed', tool_result={
                'code': 'file_page_unavailable',
                'message': '文件列表读取未完成，暂时无法取得页面。',
                'required_action': '请稍后重试查看；不能根据读取失败判断文件不存在或权限已撤销。',
            })
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
    return RegisteredTool(
        'view_file_candidates',
        ViewFileCandidatesArguments.__doc__ + f'每页最多{viewer.pool.page_size}条。',
        ViewFileCandidatesArguments,
        lambda name, raw, workspace, args: workspace,
        executor=lambda args: viewer.view(**args.model_dump()),
        return_types=('ordinary', 'foldable'),
        page_resolver=viewer.resolve_page,
        progress=ToolProgress(type='thinking', message='正在查看文件列表'),
        hooks=(render_file_candidates_feedback,),
    )
