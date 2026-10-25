"""有界工具循环；模型候选、权威提交、用户展示及私有审计分别处理。"""
import asyncio
from copy import copy,deepcopy
import re
from types import SimpleNamespace
from pydantic import ValidationError
from app.config.mllm import MLLMGenerationSettings
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.vllm_tokenizer import count_chat_tokens,count_text_tokens
from app.infrastructure.tool_tag import resolve_mllm_tool_tag,tool_tag_section
from .subgraph.context_management import compress
from .subgraph.context_management.workspace_compression import compress_workspace
from .subgraph.context_management.workspace_compression.pending import WORKSPACE_TOOLS,freeze_edit,apply_pending
from .context import build_messages,render_history,build_fixed_messages,render_workspace_section
from .context_budget import validate_count,budget
from .context_budget.fifo import measure_fifo,capacity_guidance,compression_notice
from .subgraph.context_management.workspace_compression.errors import WorkspaceEditRejected
from .json_codec import render_json
from .journal import MemoryJournal
from .model import AgentInput, AgentResult, CoreSettings
from .prompt import build_system_prompt,build_system_guidence_message
from .tool import ToolRegistry
from .context_rendering.page import render_tool_receipt
from .page_display import PageDisplayWindow
from .guidance import RequestGuidance
from collections.abc import Callable
from .output import OutputObserver

_DEFAULT_PROMPT=object()

class AgentCoreError(RuntimeError):
    """安全的运行错误；原始模型输出留在调用方持有的 journal。"""
    def __init__(self,message,*,journal=None):
        super().__init__(message)
        self.journal=journal


class AgentCore:
    def __init__(self,settings,*,client=None,counter=None,options=None,registry=None,
                 system_prompt=_DEFAULT_PROMPT,prompt_builder=None,tool_call_template=None,
                 guidance_factory: Callable[[CoreSettings], RequestGuidance] | None = None):
        if system_prompt is not _DEFAULT_PROMPT and (not isinstance(system_prompt,str) or not system_prompt.strip()):
            raise ValueError('系统提示词不能为空')
        if prompt_builder is not None and (not callable(prompt_builder) or system_prompt is not _DEFAULT_PROMPT):
            raise ValueError('提示词构建器必须可调用，且不能与完整提示词同时传入')
        if registry is not None and not isinstance(registry,ToolRegistry):
            raise TypeError('工具集合必须使用 ToolRegistry')
        if guidance_factory is not None and not callable(guidance_factory):
            raise TypeError("请求提示工厂必须可调用")
        self.guidance_factory=guidance_factory
        self.settings=settings
        self.client=client
        self.counter=counter or self._count
        self.options=CoreSettings.model_validate((options or CoreSettings()).model_dump())
        if registry is None:
            raise ValueError('必须显式装配模式工具集合，包含 finish_task 实现')
        self.registry=registry
        self.registry.require_mode_tools()
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
        registry.require_mode_tools()
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

    async def count_text(self,text,generation):
        """生产使用纯文本分词；隔离宿主沿用注入的计数器，不连接真实模型。"""
        if self.counter==self._count:
            return validate_count(await count_text_tokens(text,self.settings))
        return validate_count(await self.counter([{'role':'user','content':text}],[],generation))

    async def fifo_usage(self,request,context,accepted,generation,trajectory_budget):
        return await measure_fifo(request,context,accepted,trajectory_budget=trajectory_budget,
            count_text=lambda text:self.count_text(text,generation))

    def generation(self,effort):
        return MLLMGenerationSettings.model_validate({**self.settings.generation.model_dump(),
            'enable_thinking':effort!='none','reasoning_effort':effort if effort!='none' else 'low'})

    async def _invoke(self,client,journal,messages,tools,generation,purpose,*,json_schema=None,on_tool_delta=None):
        chunks=[]
        async def raw(chunk):
            chunks.append(deepcopy(chunk))
        try:
            response=await client.create_chat_completion(messages=deepcopy(messages),tools=deepcopy(tools),
                tool_choice='auto',generation=generation,stream=True,on_raw_chunk=raw,
                **({'json_schema':json_schema} if json_schema is not None else {}),
                **({'on_tool_delta':on_tool_delta} if on_tool_delta is not None else {}))
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

    async def _compress(self,client,journal,*,source,workspace,generation,workspace_budget,summary_limit,
                        purpose='context_compaction',focus='保留用户目标、来源、结论、冲突和待办。',protected=None):
        persistence_lock=asyncio.Lock()
        async def record_generation(*args,**kwargs):
            # 主题生成并行，但节点检查点有唯一revision；仅串行写审计，不串行模型调用。
            async with persistence_lock:
                return await journal.record_generation(*args,**kwargs)
        audit=SimpleNamespace(record_generation=record_generation)
        async def generate(messages,tools,current,**kwargs):
            return await self._invoke(client,audit,messages,tools,current,purpose,**kwargs)
        focus+=' 保留历史中用户终止、用户调整方向及执行失败的结束语义；不能把已终止问题改成待执行事项或把失败过程说成已完成。'
        return await compress(source=source,workspace=workspace,focus=focus,generate=generate,counter=self.counter,
            generation=generation,summary_generation=self.settings.generation,context_window=self.settings.context_window_tokens,
            max_errors=self.options.max_consecutive_errors,workspace_budget=workspace_budget,
            summary_limit=summary_limit,protected=protected,tool_call_template=self.tool_call_template)

    async def _compress_workspace(self,client,journal,*,workspace,generation,token_budget,
                                  protected=None,reserved_path=None):
        async def generate(messages,tools,current):
            return await self._invoke(client,journal,messages,tools,current,'workspace_compaction')
        return await compress_workspace(workspace=workspace,generate=generate,counter=self.counter,
            generation=generation,context_window=self.settings.context_window_tokens,
            max_errors=self.options.max_consecutive_errors,token_budget=token_budget,
            protected=protected,reserved_path=reserved_path,tool_call_template=self.tool_call_template)

    async def run(self,request: AgentInput,*,journal=None,publish=None,publish_progress=None,output_observer: OutputObserver | None = None) -> AgentResult:
        if output_observer is not None and any(t.deliver_output is not None for t in self.registry.entries.values()):
            raise ValueError('条件交付工具不支持流式预览，须在完整参数验收后决定发送')
        request=AgentInput.model_validate(request.model_dump())
        journal=journal if journal is not None else MemoryJournal()
        if (journal.status!='generating' or journal.exchanges or
                any(event.payload.purpose!='history_summary' for event in journal.generations)):
            raise AgentCoreError('本次运行需要全新任务；不自动重放已有任务')
        owned=self.client is None
        client=self.client or MLLMClient(self.settings)
        try:
            # 多轮探索/助理任务不设累计时限；单次依赖请求与取消清理仍各自有界。
            return await self._run(request,journal,client,publish,publish_progress,output_observer)
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

    async def _run(self,request,journal,client,publish,publish_progress,output_observer):
        self.registry.require_mode_tools()
        self.registry.require_supported_content(('ordinary','foldable'))
        pages=PageDisplayWindow(self.registry.resolve_page,max_rounds=self.options.page_display_rounds)
        try:return await self._run_loop(request,journal,client,publish,pages,publish_progress,output_observer)
        finally:
            pages.close()
            if output_observer is not None:
                try:
                    async with asyncio.timeout(self.options.cleanup_timeout_seconds):
                        await output_observer.interrupt()
                except Exception as error:
                    journal.audit.append({'event':'preview_cleanup_failed','error_type':type(error).__name__})

    async def _run_loop(self,request,journal,client,publish,pages,publish_progress,output_observer):
        # 模式状态每次运行独立创建；Core 默认不附加沟通策略。
        request_guidance=self.guidance_factory(self.options) if self.guidance_factory is not None else None
        generation=self.generation(request.thinking_effort)
        tools=self.registry.definitions()
        workspace=request.context.workspace.model_copy(deep=True)
        # 每个新任务继承事实和规划，但当前任务必须准确对应本次用户原文。
        workspace=workspace.model_copy(update={'task_constraints':workspace.task_constraints.model_copy(update={'task':request.question})})
        accepted=[]
        all_exchanges=[]
        temporary=[]
        context=request.context.model_copy(deep=True)
        errors=0
        compressions=0
        seen=set()
        intermediate=[]
        fixed=validate_count(await self.counter(self.fixed_messages(request),tools,generation))
        workspace_budget,trajectory_budget=budget(self.settings.context_window_tokens,generation.max_completion_tokens,
            self.options.management_reserve_tokens,fixed)
        await journal.commit(workspace=workspace,exchanges=[])

        async def compact(candidate,focus,purpose,protected=None,pending=()):
            nonlocal compressions
            usage=await self.fifo_usage(request,context,[*accepted,*pending],generation,trajectory_budget)
            size=usage.prefix_count
            if not size:
                raise AgentCoreError('没有可压缩的完整历史任务；保留活动任务并停止')
            if compressions>=self.options.max_compressions:
                raise AgentCoreError('上下文整理次数达到上限')
            compressions+=1
            prefix=context.model_copy(update={'turns':context.turns[:size]})
            result=await self._compress(client,journal,source=render_history(prefix),workspace=candidate,
                generation=generation,workspace_budget=workspace_budget,summary_limit=max(1,int(trajectory_budget*.7)),
                focus=focus,purpose=purpose,protected=protected)
            updated=context.model_copy(update={'summary':result.summary,'turns':context.turns[size:]})
            usage=await self.fifo_usage(request,updated,[*accepted,*pending,compression_notice()],generation,trajectory_budget)
            if usage.full:
                raise AgentCoreError('整理后仍无可继续执行的轨迹容量；不提交候选摘要')
            return result,size,updated

        def checkpoint(result,size):
            # 只覆盖继承的完整任务，绝不覆盖本轮工具对；恢复时按相同顺序消费前缀。
            return {'kind':'context_summary','text':result.summary,'covered_turns':size}

        async def commit_compaction(pending=()):
            nonlocal context,workspace,all_exchanges,accepted
            result,size,updated=await compact(workspace,'保留有效事实、来源、冲突和下一步。','context_compaction',pending=pending)
            notice=compression_notice()
            proposed=[*all_exchanges,checkpoint(result,size),notice]
            await journal.commit(workspace=result.workspace,exchanges=proposed)
            all_exchanges=proposed
            workspace=result.workspace
            context=updated
            accepted.append(notice)

        for round_number in range(1,self.options.max_rounds+1):
            usage=await self.fifo_usage(request,context,accepted,generation,trajectory_budget)
            if not temporary and usage.full:
                await commit_compaction()
            active_request=request.model_copy(update={'context':context})
            messages,shown=await pages.inject(self.build_messages(active_request,workspace=workspace,
                accepted=accepted,temporary=temporary))
            if request_guidance is not None:
                guidance=request_guidance.before_request(round_number)
                if guidance is not None:messages.append(guidance)
            count=validate_count(await self.counter(messages,tools,generation))
            if count+generation.max_completion_tokens+self.options.management_reserve_tokens>self.settings.context_window_tokens:
                raise AgentCoreError('当前上下文与预留容量超过上限')
            if output_observer is not None:await output_observer.begin()
            response=await self._invoke(client,journal,messages,tools,generation,'main',
                on_tool_delta=output_observer.on_delta if output_observer is not None else None)
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
                if output_observer is not None:await output_observer.interrupt()
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

            # 待执行调用也占容量；管理先于外部副作用，整理后重新基于权威工作区预演。
            pending=[{'role':'assistant','content':None,'tool_calls':[{'id':call.call_id,'type':'function',
                'function':{'name':call.name,'arguments':render_json(arguments.model_dump(mode='json'))}}]}]
            pending_usage=await self.fifo_usage(request,context,[*accepted,*pending],generation,trajectory_budget)
            if pending_usage.full and call.name!='compact_context':
                await commit_compaction(pending)
                arguments,candidate=self.registry.prepare(call.name,call.arguments,workspace)
            try:
                summary_result=None
                summary_size=0
                summary_context=None
                workspace_feedback=None
                workspace_guidance=None
                if call.name=='compact_context':
                    summary_result,summary_size,summary_context=await compact(candidate,arguments.focus,'context_compaction',pending=pending)
                    candidate=summary_result.workspace
                else:
                    prospective=validate_count(await self.counter([{'role':'user','content':render_workspace_section(candidate)}],[],generation))
                    if call.name in WORKSPACE_TOOLS:
                        full=prospective>=workspace_budget
                        final_tokens=prospective
                        if full:
                            if compressions>=self.options.max_compressions:
                                raise AgentCoreError('上下文整理次数达到上限')
                            compressions+=1
                            pending=freeze_edit(workspace,candidate,call.name,arguments)
                            compressed=await self._compress_workspace(client,journal,workspace=workspace,generation=generation,
                                token_budget=workspace_budget,protected=pending.protected,reserved_path=pending.reserved_path)
                            try:
                                candidate=apply_pending(compressed,pending)
                            except (ValueError,TypeError,KeyError):
                                # 这里只做纯副本重放；已知无法应用不代表外部执行或提交状态不明。
                                raise WorkspaceEditRejected('整理后无法应用原操作，候选未提交') from None
                            final_tokens=validate_count(await self.counter([{'role':'user','content':render_workspace_section(candidate)}],[],generation))
                            if final_tokens*5>=workspace_budget*4:
                                raise WorkspaceEditRejected('整理后应用本次操作仍未低于80%，不提交修改')
                        usage=f'{final_tokens}/{workspace_budget} token（{final_tokens/workspace_budget:.1%}）'
                        workspace_feedback='工作区修改成功。当前使用量：'+usage+'。'
                        if full or final_tokens*5>workspace_budget*4:
                            workspace_guidance=build_system_guidence_message(kind='context_capacity',
                                reason=('已先整理旧副本再应用本次修改，最终低于80%。' if full else '工作区使用率超过80%。')+'当前使用量：'+usage,
                                required_action=('依据最新工作区继续任务，不重放本次操作。' if full else
                                    '优先通过工作区工具主动整理至低于80%，整理期间暂停业务探索。'))
                    elif prospective>=workspace_budget:
                        candidate=await self._compress_workspace(client,journal,workspace=candidate,generation=generation,
                            token_budget=workspace_budget)
            except WorkspaceEditRejected:
                # 只有纯副本整理/重放的确定拒绝可纠错；分词、网络、落盘及未知回执仍上抛。
                if output_observer is not None:await output_observer.interrupt()
                errors+=1
                temporary.append(build_system_guidence_message(kind='output_error',
                    reason='工作区整理或重放未达到容量要求，本次修改未提交，旧工作区保持不变。',
                    required_action='请缩小本次修改或先使用工作区工具整理已有内容，再提交合法操作；不要假定失败修改已生效。'))
                journal.audit.append({'event':'rejected','tool':call.name,'code':'workspace_edit_rejected'})
                if errors>=self.options.max_consecutive_errors:
                    raise AgentCoreError('连续工具校验失败达到上限')
                continue
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
            if workspace_feedback and result.status=='succeeded':
                result=result.model_copy(update={'tool_result':{**result.tool_result,'workspace_feedback':workspace_feedback}})
            receipt={'role':'tool','tool_call_id':call.call_id,'content':render_tool_receipt(result)}
            pair=[assistant,receipt]
            guidance=[{**item.message,'source':item.source} for item in result.system_guidence]
            if result.status=='failed':
                if output_observer is not None:await output_observer.interrupt()
                errors+=1
                temporary.extend([*pair,*[dict(item.message) for item in result.system_guidence],
                    build_system_guidence_message(kind='output_error',reason='本次工具动作未成功。',
                    required_action='请根据工具反馈修正调用；无法继续时说明限制，不假定动作已完成。')])
                journal.audit.append({'event':'rejected','tool':call.name,'code':result.tool_result.get('code')})
                if errors>=self.options.max_consecutive_errors:raise AgentCoreError('连续工具校验失败达到上限')
                continue
            if workspace_guidance:
                guidance.append({**workspace_guidance,'source':'workspace'})
            accepted_action=[*pair,*guidance]
            proposed=[*all_exchanges,*accepted_action]
            if summary_result:
                proposed.append(checkpoint(summary_result,summary_size))
            output_kind=self.registry.entries[call.name].output_kind
            is_output=output_kind is not None
            delivery_condition=self.registry.entries[call.name].deliver_output
            should_deliver=delivery_condition(arguments) if delivery_condition is not None else is_output
            if type(should_deliver) is not bool:raise ValueError('输出交付条件必须返回布尔值')
            final=output_kind=='final'
            output=arguments.content if is_output else None
            # 提交异常属于状态不明或依赖故障，不能按参数纠错重试。
            output_id=await output_observer.prepare_commit(call,output) if is_output and output_observer is not None else None
            await journal.commit(workspace=candidate,exchanges=proposed,output=output,final=final,
                **({'output_id':output_id} if output_id is not None else {}))
            # 阅读位置与展示只在权威提交成功后生效；数据库错误不会提前推进游标。
            if execution.after_commit is not None:
                # 外部交付必须在权威检查点提交后进行；同步页面游标仍沿用原有回调。
                from inspect import isawaitable
                committed=execution.after_commit()
                if isawaitable(committed):await committed
            pages.register(call.call_id,result,tool_name=call.name)
            journal.audit.append({'event':'accepted','tool':call.name})
            all_exchanges=proposed
            workspace=candidate
            temporary.clear()
            errors=0
            if summary_result:
                context=summary_context
            accepted.extend(accepted_action)
            delivery_error=None
            if is_output:
                if not final:
                    intermediate.append(output)
                if should_deliver and (publish is not None or output_observer is not None):
                    try:
                        if output_observer is not None:
                            await output_observer.complete(output)
                        else:await publish('final' if final else 'intermediate',output)
                    except Exception as error:
                        delivery_error=type(error).__name__
                        journal.audit.append({'event':'delivery_failed','error_type':delivery_error})
                        if not final:
                            raise AgentCoreError('阶段输出已保存，但交付失败；停止重复执行') from None
                if request_guidance is not None and delivery_error is None:
                    # 无交付回调的内部调用以权威提交为交付；失败与取消不通知策略。
                    request_guidance.after_output(output_kind)
            if final:
                return AgentResult(answer=output,workspace=workspace,intermediate=intermediate,
                    rounds=round_number,delivery_error_type=delivery_error)
            # 成功动作已经落盘，后续容量故障只停止，不回滚或重放。提示本身也计入容量。
            base=list(accepted)
            usage=await self.fifo_usage(request,context,base,generation,trajectory_budget)
            if usage.warning:
                for _ in range(4):
                    notice=capacity_guidance(usage)
                    candidate=[*base,notice]
                    checked=await self.fifo_usage(request,context,candidate,generation,trajectory_budget)
                    if (checked.full,checked.prefix_count)==(usage.full,usage.prefix_count):break
                    usage=checked
                else:raise AgentCoreError('轨迹容量提示无法稳定；已保存成功操作并停止')
                proposed=[*all_exchanges,notice]
                await journal.commit(workspace=workspace,exchanges=proposed)
                all_exchanges=proposed
                accepted=candidate
                if checked.tokens>=checked.budget:
                    await commit_compaction()
        raise AgentCoreError('智能体工具轮数达到上限')
