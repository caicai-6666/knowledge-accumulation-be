"""有界工具循环；模型候选、权威提交、用户展示及私有审计分别处理。"""
import asyncio
from copy import copy,deepcopy
import re
from pydantic import ValidationError
from app.config.mllm import MLLMGenerationSettings
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.vllm_tokenizer import count_chat_tokens
from app.infrastructure.tool_tag import resolve_mllm_tool_tag,tool_tag_section
from .subgraph.context_management import compress
from .context import build_messages,render_history,build_fixed_messages,render_workspace_section
from .context_budget import validate_count,budget
from .json_codec import render_json
from .journal import MemoryJournal
from .model import AgentInput, AgentResult, CoreSettings
from .prompt import build_system_prompt,build_system_guidence_message
from .tool import ToolRegistry,build_registry
from .context_rendering.page import render_tool_receipt
from .page_display import PageDisplayWindow

_DEFAULT_PROMPT=object()

class AgentCoreError(RuntimeError):
    """安全的运行错误；原始模型输出留在调用方持有的 journal。"""
    def __init__(self,message,*,journal=None):
        super().__init__(message)
        self.journal=journal


class AgentCore:
    def __init__(self,settings,*,client=None,counter=None,options=None,registry=None,
                 system_prompt=_DEFAULT_PROMPT,prompt_builder=None,tool_call_template=None):
        if system_prompt is not _DEFAULT_PROMPT and (not isinstance(system_prompt,str) or not system_prompt.strip()):
            raise ValueError('系统提示词不能为空')
        if prompt_builder is not None and (not callable(prompt_builder) or system_prompt is not _DEFAULT_PROMPT):
            raise ValueError('提示词构建器必须可调用，且不能与完整提示词同时传入')
        if registry is not None and not isinstance(registry,ToolRegistry):
            raise TypeError('工具集合必须使用 ToolRegistry')
        self.settings=settings
        self.client=client
        self.counter=counter or self._count
        self.options=CoreSettings.model_validate((options or CoreSettings()).model_dump())
        self.registry=registry if registry is not None else build_registry()
        self.registry.require_supported_content(('ordinary','foldable'))
        # 无进程级可变模板，不同模型或模式的宿主不会互相污染；任务中不再读盘。
        self.tool_call_template=resolve_mllm_tool_tag(settings,tool_call_template)
        if system_prompt is _DEFAULT_PROMPT:
            self.system_prompt=(prompt_builder or build_system_prompt)(tool_call_template=self.tool_call_template)
        else:
            section=tool_tag_section(self.tool_call_template)
            self.system_prompt=system_prompt+('\n\n'+section if section else '')
        if not isinstance(self.system_prompt,str) or not self.system_prompt.strip():
            raise ValueError('系统提示词不能为空')

    def build_messages(self,request,**kwargs):
        return build_messages(request,system_prompt=self.system_prompt,**kwargs)

    def with_registry(self,registry):
        """为一次可信装配创建配置相同的宿主，不修改共享实例的工具集合。"""
        if not isinstance(registry,ToolRegistry):
            raise TypeError('工具集合必须使用 ToolRegistry')
        registry.require_supported_content(('ordinary','foldable'))
        # 仅替换工具集合，保持提示词快照不变，避免每次按用户装配时重新读盘或重复注入。
        cloned=copy(self)
        cloned.registry=registry
        return cloned

    def fixed_messages(self,request):
        """主循环与任务前整理复用同一份有效提示词和固定资料，避免预算漏计。"""
        return build_fixed_messages(request,system_prompt=self.system_prompt)

    async def _count(self,messages,tools,generation):
        return await count_chat_tokens(messages,tools,self.settings,generation=generation)

    def generation(self,effort):
        return MLLMGenerationSettings.model_validate({**self.settings.generation.model_dump(),
            'enable_thinking':effort!='none','reasoning_effort':effort if effort!='none' else 'low'})

    async def _invoke(self,client,journal,messages,tools,generation,purpose):
        chunks=[]
        async def raw(chunk):
            chunks.append(deepcopy(chunk))
        try:
            response=await client.create_chat_completion(messages=deepcopy(messages),tools=deepcopy(tools),
                tool_choice='auto',generation=generation,stream=True,on_raw_chunk=raw)
        except BaseException as error:
            # 不自动重放请求；取消也先尽力保留已接收分片。持久化失败不掩盖原异常。
            try:
                async with asyncio.timeout(self.options.cleanup_timeout_seconds):
                    await journal.record_generation(status='cancelled' if isinstance(error,asyncio.CancelledError) else 'failed',
                        purpose=purpose,raw_chunks=chunks,error_type=type(error).__name__)
            except Exception:
                pass
            raise
        await journal.record_generation(response,purpose=purpose)
        return response

    async def _compress(self,client,journal,*,source,workspace,generation,workspace_limit,summary_limit,
                        purpose='context_compaction',focus='保留用户目标、来源、结论、冲突和待办。',protected=None):
        async def generate(messages,tools,current):
            return await self._invoke(client,journal,messages,tools,current,purpose)
        focus+=' 保留历史中用户终止、用户调整方向及执行失败的结束语义；不能把已终止问题改成待执行事项或把失败过程说成已完成。'
        return await compress(source=source,workspace=workspace,focus=focus,generate=generate,counter=self.counter,
            generation=generation,context_window=self.settings.context_window_tokens,
            max_errors=self.options.max_consecutive_errors,workspace_limit=workspace_limit,
            summary_limit=summary_limit,protected=protected,tool_call_template=self.tool_call_template)

    async def run(self,request: AgentInput,*,journal=None,publish=None,publish_progress=None) -> AgentResult:
        request=AgentInput.model_validate(request.model_dump())
        journal=journal if journal is not None else MemoryJournal()
        if (journal.status!='generating' or journal.exchanges or
                any(event.payload.purpose!='history_summary' for event in journal.generations)):
            raise AgentCoreError('本次运行需要全新任务；不自动重放已有任务')
        owned=self.client is None
        client=self.client or MLLMClient(self.settings)
        try:
            async with asyncio.timeout(self.options.timeout_seconds):
                return await self._run(request,journal,client,publish,publish_progress)
        except BaseException as error:
            try:
                async with asyncio.timeout(self.options.cleanup_timeout_seconds):
                    await journal.abort('cancelled' if isinstance(error,asyncio.CancelledError) else 'failed')
            except Exception as cleanup:
                journal.audit.append({'event':'cleanup_failed','error_type':type(cleanup).__name__})
            if isinstance(error,asyncio.CancelledError):
                raise
            if isinstance(error,AgentCoreError):
                error.journal=journal
                raise
            raise AgentCoreError('智能体执行失败；已停止继续调用',journal=journal) from None
        finally:
            if owned:
                await client.close()

    async def _run(self,request,journal,client,publish,publish_progress):
        self.registry.require_supported_content(('ordinary','foldable'))
        pages=PageDisplayWindow(self.registry.resolve_page,max_rounds=self.options.page_display_rounds)
        try:return await self._run_loop(request,journal,client,publish,pages,publish_progress)
        finally:pages.close()

    async def _run_loop(self,request,journal,client,publish,pages,publish_progress):
        generation=self.generation(request.thinking_effort)
        tools=self.registry.definitions()
        workspace=request.context.workspace.model_copy(deep=True)
        # 每个新任务继承事实和规划，但当前任务必须准确对应本次用户原文。
        workspace=workspace.model_copy(update={'task_constraints':workspace.task_constraints.model_copy(update={'task':request.question})})
        accepted=[]
        all_exchanges=[]
        temporary=[]
        local_summary=None
        errors=0
        compressions=0
        seen=set()
        intermediate=[]
        fixed=validate_count(await self.counter(self.fixed_messages(request),tools,generation))
        workspace_budget,trajectory_budget=budget(self.settings.context_window_tokens,generation.max_completion_tokens,
            self.options.management_reserve_tokens,fixed)
        await journal.commit(workspace=workspace,exchanges=[])

        async def compact(candidate,focus,purpose,protected=None):
            nonlocal compressions
            if compressions>=self.options.max_compressions:
                raise AgentCoreError('上下文整理次数达到上限')
            compressions+=1
            source={'history':render_history(request.context) if local_summary is None else {'summary':local_summary},
                'accepted_interactions':accepted,'current_question':request.question}
            return await self._compress(client,journal,source=source,workspace=candidate,generation=generation,
                workspace_limit=max(1,int(workspace_budget*.7)),summary_limit=max(1,int(trajectory_budget*.7)),
                focus=focus,purpose=purpose,protected=protected)

        for round_number in range(1,self.options.max_rounds+1):
            messages,shown=await pages.inject(self.build_messages(request,workspace=workspace,
                summary=local_summary,accepted=accepted,temporary=temporary))
            count=validate_count(await self.counter(messages,tools,generation))
            workspace_count=validate_count(await self.counter([{'role':'user','content':render_workspace_section(workspace)}],[],generation))
            if not temporary and (count+generation.max_completion_tokens+self.options.management_reserve_tokens>=
                                  self.settings.context_window_tokens*self.options.soft_limit or workspace_count>=workspace_budget):
                result=await compact(workspace,'保留当前任务、有效事实、来源、冲突和下一步。','context_compaction')
                proposed=[*all_exchanges,{'kind':'context_summary','text':result.summary}]
                await journal.commit(workspace=result.workspace,exchanges=proposed)
                all_exchanges=proposed
                workspace=result.workspace
                local_summary=result.summary
                accepted=pages.active_pairs(accepted)
                messages,shown=await pages.inject(self.build_messages(request,workspace=workspace,
                    summary=local_summary,accepted=accepted))
                count=validate_count(await self.counter(messages,tools,generation))
            if count+generation.max_completion_tokens+self.options.management_reserve_tokens>self.settings.context_window_tokens:
                raise AgentCoreError('当前上下文与预留容量超过上限')
            response=await self._invoke(client,journal,messages,tools,generation,'main')
            pages.consume(shown,finish_reason=response.finish_reason)
            if journal.status!='generating':
                raise AgentCoreError('任务已结束，丢弃迟到业务动作')
            try:
                if (len(response.tool_calls)!=1 or response.content not in (None,'') or response.assistant_message.get('refusal')
                        or response.finish_reason not in ('stop','tool_calls')):
                    raise ValueError('每次必须提交一个完整原生工具调用，不能附带普通文本')
                call=response.tool_calls[0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',call.call_id) or call.call_id in seen:
                    raise ValueError('原生调用标识不合法或重复')
                seen.add(call.call_id)
                arguments,candidate=self.registry.prepare(call.name,call.arguments,workspace)
            except (ValueError,TypeError) as error:
                errors+=1
                # 完整错误响应仅在私有生成；不给模型回显无效正文或参数。
                location='、'.join('.'.join(map(str,e['loc'])) for e in error.errors()[:5]) if isinstance(error,ValidationError) else ''
                reason='工具参数不符合定义，请检查 '+location if location else '需要一个可用工具，且参数、路径、引用和当前状态必须合法。'
                temporary.append(build_system_guidence_message(kind='output_error',reason=reason+' 本次动作未提交。',
                    required_action='请按当前工具 Schema 修正参数、路径或引用，并只调用一个工具。'+
                        ('\n'+tool_tag_section(self.tool_call_template) if self.tool_call_template else '')))
                journal.audit.append({'event':'rejected','error_type':type(error).__name__,'fields':location})
                if errors>=self.options.max_consecutive_errors:
                    raise AgentCoreError('连续工具校验失败达到上限')
                continue

            summary_result=None
            if call.name=='compact_context':
                summary_result=await compact(candidate,arguments.focus,'context_compaction')
                candidate=summary_result.workspace
            else:
                prospective=validate_count(await self.counter([{'role':'user','content':render_workspace_section(candidate)}],[],generation))
                if prospective>=workspace_budget:
                    before=workspace.model_dump();after=candidate.model_dump()
                    protected={region:{key:value for key,value in after[region].items() if before[region].get(key)!=value}
                        for region in ('known_information','explored_directions','remaining_directions')}
                    summary_result=await compact(candidate,'精简工作区并保持本次修改的目标条目。','workspace_compaction',protected)
                    candidate=summary_result.workspace
            assistant={'role':'assistant','content':None,'tool_calls':[{'id':call.call_id,'type':'function',
                'function':{'name':call.name,'arguments':render_json(arguments.model_dump(mode='json'))}}]}
            # 执行或提交故障直接停止，不当作模型参数错误反复调用外部服务。
            try:
                execution=await self.registry.execute(call.name,arguments,publish_progress=publish_progress)
                result=execution.result
                pages.validate(call.call_id,result)
            except BaseException:
                journal.audit.append({'event':'execution_unknown','tool':call.name})
                raise
            if result.status=='unknown':
                journal.audit.append({'event':'execution_unknown','tool':call.name})
                raise AgentCoreError('工具执行结果不确定；已停止且不自动重放')
            receipt={'role':'tool','tool_call_id':call.call_id,'content':render_tool_receipt(result)}
            pair=[assistant,receipt]
            guidance=[{**item.message,'source':item.source} for item in result.system_guidence]
            if result.status=='failed':
                errors+=1
                temporary.extend([*pair,*[dict(item.message) for item in result.system_guidence],
                    build_system_guidence_message(kind='output_error',reason='本次工具动作未成功。',
                    required_action='请根据工具反馈修正调用；无法继续时说明限制，不假定动作已完成。')])
                journal.audit.append({'event':'rejected','tool':call.name,'code':result.tool_result.get('code')})
                if errors>=self.options.max_consecutive_errors:raise AgentCoreError('连续工具校验失败达到上限')
                continue
            accepted_action=[*pair,*guidance]
            proposed=[*all_exchanges,*accepted_action]
            if summary_result:
                proposed.append({'kind':'context_summary','text':summary_result.summary})
            is_output=call.name in ('emit_progress','finish_task')
            final=call.name=='finish_task'
            output=arguments.content if is_output else None
            # 提交异常属于状态不明或依赖故障，不能按参数纠错重试。
            await journal.commit(workspace=candidate,exchanges=proposed,output=output,final=final)
            # 阅读位置与展示只在权威提交成功后生效；数据库错误不会提前推进游标。
            if execution.after_commit is not None:execution.after_commit()
            pages.register(call.call_id,result,tool_name=call.name)
            journal.audit.append({'event':'accepted','tool':call.name})
            all_exchanges=proposed
            workspace=candidate
            temporary.clear()
            errors=0
            if summary_result:
                local_summary=summary_result.summary
                # 保留本次成功回执作为继续点，否则“先整理”请求会因看不到已执行而循环整理。
                accepted=[*pages.active_pairs(accepted),*accepted_action]
            else:
                accepted.extend(accepted_action)
            delivery_error=None
            if is_output:
                if not final:
                    intermediate.append(output)
                if publish is not None:
                    try:
                        await publish('final' if final else 'intermediate',output)
                    except Exception as error:
                        delivery_error=type(error).__name__
                        journal.audit.append({'event':'delivery_failed','error_type':delivery_error})
                        if not final:
                            raise AgentCoreError('阶段输出已保存，但交付失败；停止重复执行') from None
            if final:
                return AgentResult(answer=output,workspace=workspace,intermediate=intermediate,
                    rounds=round_number,delivery_error_type=delivery_error)
        raise AgentCoreError('智能体工具轮数达到上限')
