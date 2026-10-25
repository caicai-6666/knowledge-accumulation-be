"""主模型文件检索入口；结果通过journal提交后发布，失败不泄漏内部集合。"""
import asyncio
import logging
from contextlib import AsyncExitStack
from langgraph.errors import NodeCancelledError
from ..subgraph.file_retrieval.schema import SearchFilesArguments
from ..subgraph.file_retrieval.pool import CandidatePool
from ..subgraph.file_retrieval.ranking import rank_candidates
from ..subgraph.file_retrieval import build_file_retrieval_subgraph
from .registry import RegisteredTool
from .result import ToolExecution, ToolExecutionResult
from .progress import ToolProgress


def build_file_search_registration(*, pool, backend, userid, core, journal, thinking_effort):
    async def execute(args):
        candidates=None
        try:
            parent=pool.get(args.result_id) if args.result_id is not None else None
            platforms=tuple(args.platform_codes) if args.platform_codes is not None else None
            if parent is not None and parent.platforms is not None:
                platforms=parent.platforms if platforms is None else tuple(p for p in platforms if p in parent.platforms)
            initial=tuple(i for i,_ in parent.ranking.hits) if parent is not None else None
            # 本次初始范围由权威来源确定；关闭任务、撤权或平台交集为空都不能退回全库。
            if pool.closed:
                raise ValueError('查询已释放')
            rows=await backend.read(userid,initial,platforms)
            allowed=tuple(r.id for r in rows)
            if parent is not None:
                pool.check(args.result_id,parent)
            if pool.closed:
                raise ValueError('查询已释放')
            if not allowed:
                return ToolExecutionResult(status='succeeded',tool_result={'status':'success','count':0,
                    'explanation':'当前限定范围内没有可访问文件，未生成结果集。'})
            previous=rank_candidates(tuple((i,1.) for i in allowed),search_type='exact',parent=parent.ranking,exact=True) if parent else None
            settings=backend.settings
            candidates=CandidatePool(initial=allowed,initial_ranking=previous,capacity=settings.cache_max_queries,
                k=settings.rrf_k,history_weight=settings.history_weight)
            async def search(field,query,scope):
                if pool.closed:
                    raise ValueError('查询已释放')
                if parent is not None:
                    pool.check(args.result_id,parent)
                result=await backend.search(userid,field,query,scope,platforms)
                if pool.closed:
                    raise ValueError('查询已释放')
                if parent is not None:
                    pool.check(args.result_id,parent)
                return result
            async with AsyncExitStack() as stack:
                from app.infrastructure.mllm import MLLMClient
                client=core.client if core.client is not None else await stack.enter_async_context(MLLMClient(core.settings))
                async def generate(messages,tools,generation):
                    return await core._invoke(client,journal(),messages,tools,generation,'file_retrieval')
                graph=build_file_retrieval_subgraph(pool=candidates,search=search,generate=generate,counter=core.counter,
                    generation=core.generation(thinking_effort),context_window=core.settings.context_window_tokens,
                    tool_template=core.tool_call_template,max_rounds=settings.max_rounds,
                    audit=lambda event:journal().record_file_retrieval(event))
                ranking,explanation=(await graph.ainvoke({'query':args.query}))['result']
            if parent is not None:
                pool.check(args.result_id,parent)
            if pool.closed:
                raise ValueError('查询已释放')
            if ranking is None:
                return ToolExecutionResult(status='succeeded',tool_result={'status':'success','count':0,'explanation':explanation})
            current=await backend.read(userid,tuple(i for i,_ in ranking.hits),platforms)
            ids={r.id for r in current}
            if not ids:
                return ToolExecutionResult(status='succeeded',tool_result={'status':'success','count':0,
                    'explanation':'候选已不可访问，请重新检索。'})
            ranking=rank_candidates(tuple((i,1.) for i in ids),search_type='exact',parent=ranking,exact=True)
            rid,commit=pool.prepare(ranking,platforms,explanation,parent_id=args.result_id,parent=parent)
            return ToolExecution(ToolExecutionResult(status='succeeded',tool_result={
                'status':'success','result_id':rid,'count':len(ranking.hits),'explanation':explanation}),after_commit=commit)
        except NodeCancelledError:
            raise asyncio.CancelledError() from None
        except Exception as error:
            logging.getLogger(__name__).warning('文件检索失败：error_type=%s',type(error).__name__)
            return ToolExecutionResult(status='failed',tool_result={'code':'file_search_unavailable',
                'message':'结果引用已失效、范围不合法或文件检索依赖暂不可用；本次查询失败，不能解释为没有相关文件。'})
        finally:
            if candidates is not None:
                candidates.close()
    def progress(args):
        # 只压缩展示空白并截断状态长度，实际检索保留完整 query 和条件关系。
        text=''.join(char for char in ' '.join(args.query.split()) if char.isprintable())
        prefix='正在检索注册中心文件：'
        limit=2000-len(prefix)
        if len(text)>limit:text=text[:limit-1]+'…'
        return ToolProgress(type='local-search',message=prefix+text)

    return RegisteredTool('search_files',SearchFilesArguments.__doc__,SearchFilesArguments,
        lambda name,raw,workspace,args:workspace,executor=execute,
        progress=ToolProgress(type='local-search',message='正在检索注册中心文件'),progress_factory=progress)
