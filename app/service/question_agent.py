"""树状上下文与 Agent Core 组合入口；无 HTTP、启动任务或真实配置装配副作用。"""
import asyncio
import logging
from inspect import isawaitable
from pydantic import TypeAdapter
from app.agent.core import AgentInput, AgentResult, ContextInjection, MemoryJournal, NodeJournal
from app.agent.context import build_tree_context
from app.agent.core.context_budget import budget,validate_count
from app.agent.core.context import render_history
from app.agent.core.tool import ToolRegistry,build_image_view_registration
from app.agent.core.file import FileModelCache,ImageFileViewer
from app.agent.core.file.registry_viewer import RegistryFileViewer
from app.agent.core.tool.registry_file import build_registry_file_registration
from app.agent.core.expert import ExpertSessions
from app.agent.core.web import WebSessions
from app.agent.core.tool.sub_management_platform import SubManagementPlatformViewer, build_sub_management_platform_registration
from app.agent.core.tool.external_expert import build_external_expert_registrations
from app.agent.core.subgraph.memory_retrieval.pool import MemoryQueryPool
from app.agent.core.tool.memory_search import build_memory_search_registration
from app.agent.core.tool.memory_viewer import MemoryQueryViewer, build_memory_view_registration
from app.agent.core.subgraph.file_retrieval.pool import FileResultPool
from app.agent.core.tool.file_search import build_file_search_registration
from app.agent.core.tool.file_candidates import FileCandidateViewer, build_file_candidates_registration
from app.auth.model import AuthenticatedUser
from app.infrastructure.mllm import MLLMClient
from app.infrastructure.vllm_tokenizer import TokenizationUnavailable
from app.model.question_tree import SummaryContent, TreeID, TreeModel, WorkspaceVersion, new_tree_id
from app.model.question_file import UploadedFile
from app.repository.question_tree import QuestionTreeConflict

logger=logging.getLogger(__name__)


class ModelPreparationUnavailable(RuntimeError):
    """助理尚未登记节点或执行生成，模型服务临时不可用，可保留队列任务重试。"""


class TreeAgentResult(TreeModel):
    tree_id: TreeID
    node_id: TreeID
    summary_id: TreeID | None = None
    result: AgentResult


class QuestionAgentService:
    def __init__(self,nodes,core,*,context_builder=None,expected_tree_kind=None,tool_loader=None,file_service=None,file_cache=None,
                 expert_settings=None,expert_client=None,web_search_service=None,web_graph_factory=None,memory_backend=None,platform_directory=None,file_retrieval=None,registry_files=None,registry_file_access=None,output_observer_factory=None,platform_a2a=None,registry_builder=None,file_authorize_wrapper=None):
        self.platform_a2a=platform_a2a
        self.output_observer_factory=output_observer_factory
        self.nodes=nodes
        self.core=core
        self.context_builder=context_builder
        self.registry_builder=registry_builder
        self.expected_tree_kind=expected_tree_kind
        self.tool_loader=tool_loader
        self.memory_backend=memory_backend
        self.platform_directory=platform_directory
        self.file_retrieval=file_retrieval
        self.registry_files=registry_files
        self.registry_file_access=registry_file_access
        self.file_service=file_service
        # 可选策略仅包装上传附件查看器，不影响通用读取服务及注册中心授权。
        self.file_authorize_wrapper=file_authorize_wrapper
        if file_cache is not None and ((file_service is None and registry_files is None) or not isinstance(file_cache,FileModelCache)):
            raise ValueError('外部文件池需要与上传文件或注册中心文件服务一起装配')
        self.file_cache=(file_cache or FileModelCache(max_files=core.options.file_cache_max_files)) if file_service is not None or registry_files is not None else None
        self._owns_file_cache=(file_service is not None or registry_files is not None) and file_cache is None
        self._running={}
        self._control_lock=asyncio.Lock()
        self._closed=False
        self.experts=ExpertSessions(settings=expert_settings,client=expert_client,
            capacity=core.options.expert_session_cache_max_sessions)
        self.web=WebSessions(settings=core.settings,options=core.options,search_service=web_search_service,
            graph_factory=web_graph_factory,client=core.client)

    async def generate(self,*,user:AuthenticatedUser,tree_id:str,question:str,selected_node_id=None,
                       context_injection:str='',attachments=(),uploads=(),thinking_effort='xhigh',publish=None,publish_progress=None,publish_stream=None,node_id=None,
                       supersedes_node_id=None,advance_main=None,expected_revision=None,on_registered=None,resume_registered=False):
        # 只有显式装配展示适配器的模式才接受流式订阅，验收发生在附件/节点写入之前。
        if publish_stream is not None and self.output_observer_factory is None:
            raise ValueError('当前问答模式未配置流式输出')
        user=AuthenticatedUser.model_validate(user.model_dump())
        node_id=TypeAdapter(TreeID).validate_python(node_id or new_tree_id())
        # 替代是写操作，所有普通输入必须先验收，不能先停止旧任务再发现新要求非法。
        validated=AgentInput.for_user(user,question=question,attachments=attachments,thinking_effort=thinking_effort,
            context=ContextInjection(supplemental=context_injection))
        uploads=TypeAdapter(list[UploadedFile]).validate_python(uploads)
        if supersedes_node_id is not None and selected_node_id is not None:
            raise ValueError('替代任务由服务端绑定旧节点，不能同时选择其他起点')
        if resume_registered and (supersedes_node_id is not None or uploads):
            raise ValueError('待激活接替节点不再次替代或上传附件')
        if self._closed:raise RuntimeError('问答服务已关闭')
        references=validated.attachments
        if uploads or references:
            if self.file_service is None:raise ValueError('附件生成需要显式装配文件服务')
            references=await self.file_service.prepare(tree_id,user.userid,uploads=uploads,references=references)
        created=[r for r in references if r not in validated.attachments]
        registered=None
        if resume_registered:
            registered=await self.nodes.load_node(tree_id,user.userid,node_id)
            if (registered.node.status!='generating' or registered.content.question!=question
                    or registered.content.attachments!=list(references) or registered.content.exchanges
                    or registered.generations or len(registered.messages)!=1):
                raise QuestionTreeConflict('仅允许激活尚未执行的接替节点')
        if supersedes_node_id is not None:
            registered=await self.supersede(user=user,tree_id=tree_id,node_id=supersedes_node_id,
                replacement_id=node_id,question=question,attachments=references)
        key=(user.userid,tree_id,node_id)
        task=asyncio.current_task()
        owns_run=False
        viewer=None
        registry_viewer=None
        memory_pool=None
        platform_viewer=None
        file_results=None
        try:
            async with self._control_lock:
                if self._closed:
                    if registered is not None:
                        await self.nodes.fail_task(tree_id,user.userid,node_id)
                    raise RuntimeError('问答服务已关闭')
                if key in self._running:
                    raise QuestionTreeConflict('该节点已有正在执行的请求')
                self._running[key]=task
                owns_run=True
            if self.file_service is not None:
                async def read_file(file_id):return await self.file_service.read_file(tree_id,user.userid,file_id)
                async def authorize(file_id):return await self.nodes.find_file(tree_id,user.userid,file_id)
                if self.file_authorize_wrapper is not None:
                    authorize=self.file_authorize_wrapper(authorize, user)
                viewer=ImageFileViewer(scope=(user.userid,tree_id),read_file=read_file,authorize=authorize,cache=self.file_cache)
            if self.registry_files is not None:
                async def authorize_registry_file(file_id):
                    return await self.registry_file_access.authorize(file_id, userid=user.userid)
                registry_viewer=RegistryFileViewer(scope=(user.userid,tree_id),client=self.registry_files,
                    cache=self.file_cache,
                    authorize=authorize_registry_file if self.registry_file_access is not None else None)
            if self.memory_backend is not None:
                memory_pool=MemoryQueryPool(lambda ids:self.memory_backend.read(tree_id,user.userid,node_id,ids),
                    capacity=self.core.options.memory_cache_max_queries,page_size=self.core.options.memory_page_size)
            if self.file_retrieval is not None:
                file_results=FileResultPool(lambda ids,platforms:self.file_retrieval.read(user.userid,ids,platforms),
                    capacity=self.file_retrieval.settings.cache_max_queries,page_size=self.file_retrieval.settings.page_size)
            if self.platform_directory is not None:
                platform_viewer=SubManagementPlatformViewer(self.platform_directory)
            with self.web.lease(user.userid,tree_id) as resources:
                return await self._generate(user=user,tree_id=tree_id,question=question,selected_node_id=selected_node_id,
                    context_injection=context_injection,thinking_effort=thinking_effort,publish=publish,publish_stream=publish_stream,node_id=node_id,
                    attachments=references,_registered=registered,_viewer=viewer,advance_main=advance_main,
                    expected_revision=expected_revision,on_registered=on_registered,_web=resources,publish_progress=publish_progress,
                    _memory=memory_pool,_platform_viewer=platform_viewer,_file_results=file_results,_registry_viewer=registry_viewer)
        except BaseException as error:
            # 只有数据库明确确认未登记时才补偿文件；提交回执不明不能删除可能已被节点引用的文件。
            if created:
                await self.file_service.cleanup_unregistered(tree_id,user.userid,created)
            if registered is not None and owns_run:
                try:
                    async with asyncio.timeout(self.core.options.cleanup_timeout_seconds):
                        operation=self.nodes.cancel_task if isinstance(error,asyncio.CancelledError) and not self._closed else self.nodes.fail_task
                        await operation(tree_id,user.userid,node_id)
                except Exception:
                    pass  # 其他终态先提交时不能覆盖；依赖不可用时也不能伪称已完成清理。
            raise
        finally:
            if registry_viewer is not None:await registry_viewer.aclose()
            if file_results is not None:file_results.close()
            if platform_viewer is not None:platform_viewer.close()
            if memory_pool is not None:memory_pool.close()
            if viewer is not None:await viewer.aclose()
            if self._running.get(key) is task:
                self._running.pop(key)

    async def _join(self,task):
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            _,pending=await asyncio.wait({task},timeout=self.core.options.cleanup_timeout_seconds)
            if pending:
                logger.warning('任务控制已提交，生产协程尚未结束')
            elif not task.cancelled():
                task.exception()  # 控制方也收集结束异常；原请求仍可照常 await 同一任务。

    async def cancel(self,*,user:AuthenticatedUser,tree_id,node_id):
        async with self._control_lock:
            if self._closed:
                raise RuntimeError('问答服务已关闭')
            await self.nodes.cancel_task(tree_id,user.userid,node_id)
            task=self._running.get((user.userid,tree_id,node_id))
        await self._join(task)
        return await self.nodes.load_node(tree_id,user.userid,node_id)

    async def supersede(self,*,user:AuthenticatedUser,tree_id,node_id,replacement_id,question,attachments=()):
        async with self._control_lock:
            if self._closed:
                raise RuntimeError('问答服务已关闭')
            current=await self.nodes.load_node(tree_id,user.userid,node_id)
            if self.expected_tree_kind is not None and current.tree.kind!=self.expected_tree_kind:
                raise ValueError('树用途与已装配的生成服务不一致')
            result=await self.nodes.replace_task(tree_id,user.userid,node_id,replacement_id=replacement_id,question=question,
                attachments=attachments)
            task=self._running.get((user.userid,tree_id,node_id))
        await self._join(task)
        return result

    async def close(self):
        async with self._control_lock:
            self._closed=True
            active=list(self._running.items())
            for (owner,tree_id,node_id),_ in active:
                try:
                    await self.nodes.fail_task(tree_id,owner,node_id)
                except Exception as error:
                    logger.warning('问答停机状态清理未确认：error_type=%s',type(error).__name__)
        await asyncio.gather(*(self._join(task) for _,task in active))
        await self.experts.close()
        self.web.close()
        if self._owns_file_cache:await self.file_cache.aclose()

    def release_tree(self, tree_id, owner):
        """删除树前由宿主等待生产者，再释放该用户树绑定的专家会话。"""
        self.experts.release(owner, tree_id)
        self.web.release(owner, tree_id)

    async def _generate(self,*,user:AuthenticatedUser,tree_id:str,question:str,selected_node_id=None,
                       context_injection:str='',attachments=(),thinking_effort='xhigh',publish=None,publish_progress=None,publish_stream=None,node_id=None,_registered=None,_viewer=None,
                       advance_main=None,expected_revision=None,on_registered=None,_web=None,_memory=None,_platform_viewer=None,_file_results=None,_registry_viewer=None):
        # 等级、归属来自可信身份；外部注入只作为普通资料，不接收 system/tool 角色。
        user=AuthenticatedUser.model_validate(user.model_dump())
        node_id=TypeAdapter(TreeID).validate_python(node_id or new_tree_id())
        core=self.core
        def cancellation_status():
            # 停机取消不代表用户手动停止；整轮生成与历史摘要准备均不设累计时限。
            return 'failed' if self._closed else 'cancelled'
        if _registered is not None:
            selected_node_id=_registered.node.parent_id
        tree,path,writes,workspace,outputs=await self.nodes.load_agent_context(tree_id,user.userid,selected_node_id)
        if expected_revision is not None and tree.revision!=expected_revision:
            raise QuestionTreeConflict('树已变化，请重新注册任务')
        if self.expected_tree_kind is not None and tree.kind!=self.expected_tree_kind:
            raise ValueError('树用途与已装配的生成服务不一致')
        if tree.kind=='assistant' and selected_node_id is not None and _registered is None:
            raise ValueError('助理生成入口不接受节点选择')
        if self.registry_builder is not None:
            # 模式在任务局部绑定可信身份，避免并发用户互相覆盖工具校验条件。
            core=core.with_registry(self.registry_builder(user.model_copy(deep=True),core.registry,tree_id))
        additional=[build_image_view_registration(_viewer)] if _viewer is not None else []
        if _registry_viewer is not None:
            additional.append(build_registry_file_registration(_registry_viewer))
        async def record_expert(event):
            # 只有节点登记完成后才运行工具；闭包始终指向本轮节点的 journal。
            async with asyncio.timeout(core.options.cleanup_timeout_seconds):
                await journal.record_expert(event)
        additional.extend(build_external_expert_registrations(
            get_pool=lambda:self.experts.get(user.userid,tree_id),on_audit=record_expert))
        async def record_web(event):
            async with asyncio.timeout(core.options.cleanup_timeout_seconds):
                await journal.record_web_refinement(event)
        if _web is not None:
            additional.extend(_web.tools(self.web.service,on_audit=record_web))
        if _memory is not None:
            additional.extend([build_memory_search_registration(pool=_memory,
                search=lambda plan:self.memory_backend.search(tree_id,user.userid,node_id,plan),
                reference_time=lambda:journal.restored.node.created_at, core=core, journal=lambda:journal,
                thinking_effort=thinking_effort), build_memory_view_registration(MemoryQueryViewer(_memory))])
            if tree.kind == 'assistant':
                from app.agent.assistant.tool.memory_window import build_memory_window_registration
                additional.append(build_memory_window_registration(pool=_memory,
                    lookup=lambda **args:self.memory_backend.window(tree_id,user.userid,node_id,**args)))
        if _file_results is not None:
            additional.extend([build_file_search_registration(pool=_file_results,backend=self.file_retrieval,
                userid=user.userid,core=core,journal=lambda:journal,thinking_effort=thinking_effort),
                build_file_candidates_registration(FileCandidateViewer(_file_results))])
        if _platform_viewer is not None:
            additional.append(build_sub_management_platform_registration(_platform_viewer))
        if self.platform_a2a is not None:
            from app.agent.core.tool.platform_a2a import build_platform_a2a_registrations
            additional.extend(build_platform_a2a_registrations(self.platform_a2a,
                userid=user.userid,tree_id=tree_id,node_id=node_id))
        if self.tool_loader is not None:
            loaded=self.tool_loader(user.model_copy(deep=True))
            loaded=await loaded if isawaitable(loaded) else loaded
            # 本轮专用工具只存于局部宿主，不能给共享实例换上另一个用户的执行器。
            additional.extend(loaded)
        if additional:core=core.with_registry(ToolRegistry([*core.registry.entries.values(),*additional]))
        # 统一使用一致性读取实际选定的起点；摘要、成功提交和失败留痕必须绑定同一节点。
        selected_node_id=path.nodes[-1].id if path is not None else None
        context=build_tree_context(writes,workspace,outputs,supplemental=context_injection,
            summary_text=path.summary_text if path is not None else None)
        if self.context_builder is not None:
            enriched=self.context_builder(user.model_copy(deep=True),context.model_copy(deep=True))
            context=await enriched if isawaitable(enriched) else enriched
            context=ContextInjection.model_validate(context.model_dump())
        request=AgentInput.for_user(user,question=question,attachments=attachments,thinking_effort=thinking_effort,context=context)
        if _registered is not None:
            request=request.model_copy(update={'created_at':_registered.node.created_at})
        prepared=MemoryJournal(node_id)
        summary_content=None
        summary_id=None
        preparing=True
        try:
            generation=core.generation(request.thinking_effort)
            tools=core.registry.definitions()
            fixed=validate_count(await core.counter(core.fixed_messages(request),tools,generation))
            wb,tb=budget(core.settings.context_window_tokens,generation.max_completion_tokens,
                core.options.management_reserve_tokens,fixed)
            usage=await core.fifo_usage(request,context,[],generation,tb)
            preparing=False
            # 结构摘要覆盖到所选父节点；部分前缀交给Core保存精确覆盖检查点，不能假报整链已摘要。
            if (path is not None and _registered is None and usage.full
                    and usage.prefix_count==len(context.turns) and context.turns):
                owned=core.client is None
                client=core.client or MLLMClient(core.settings)
                try:
                    result=await core._compress(client,prepared,source=render_history(context),
                        workspace=request.context.workspace,generation=generation,workspace_budget=wb,
                        summary_limit=max(1,int(tb*.7)),purpose='history_summary')
                finally:
                    if owned:
                        await client.close()
                updated=context.model_copy(update={'summary':result.summary,'turns':[],'workspace':result.workspace})
                checked=await core.fifo_usage(request,updated,[],generation,tb)
                if checked.full:raise ValueError('整理后仍无可继续执行的轨迹容量')
                summary_id=new_tree_id()
                summary_content=SummaryContent(text=result.summary,covered_from_id=path.nodes[0].id,
                    covered_to_id=selected_node_id)
                request=request.model_copy(update={'context':ContextInjection(summary=result.summary,
                    workspace=result.workspace,supplemental=context.supplemental)})
        except BaseException as error:
            if (tree.kind=='assistant' and _registered is None and preparing
                    and isinstance(error,TokenizationUnavailable) and not prepared.generations):
                # 此时尚未登记节点、生成摘要或执行工具，不能把依赖未就绪当作业务失败消费。
                # 只允许明确的临时分词故障重试；认证、格式、容量和后续执行错误沿用终态策略。
                raise ModelPreparationUnavailable('模型准备阶段暂时不可用，任务尚未执行') from error
            # 摘要失败仍保留本次提问及已生成的私有审计，不提交半份摘要或改变父节点。
            try:
                async with asyncio.timeout(core.options.cleanup_timeout_seconds):
                    restored=await self.nodes.start_task(tree_id,user.userid,node_id=node_id,question=question,
                        parent_id=selected_node_id,expected_revision=tree.revision,attachments=attachments,advance_main=advance_main)
                    journal=NodeJournal(self.nodes,restored,cancellation_status=cancellation_status)
                    for event in prepared.generations:
                        await journal._save_generation(event)
                    await journal.abort(cancellation_status() if isinstance(error,asyncio.CancelledError) else 'failed')
                    if on_registered is not None:await on_registered(journal.restored,None)
            except Exception:
                pass
            raise
        restored=await self.nodes.start_task(tree_id,user.userid,node_id=node_id,question=question,
            parent_id=selected_node_id,expected_revision=tree.revision,summary=summary_content,summary_id=summary_id,
            attachments=attachments,advance_main=advance_main,
            **({'summary_workspace':WorkspaceVersion(tree_id=tree_id,content=request.context.workspace.model_dump(mode='json'))}
               if tree.kind=='assistant' and summary_content is not None else {}))
        journal=NodeJournal(self.nodes,restored,cancellation_status=cancellation_status)
        # 驱逐已在同事务提交；释放宿主准备阶段对旧窗口的引用，不延长整轮任务的驻留时间。
        path=writes=workspace=outputs=context=None
        # 首次创建、预登记激活和恢复均以仓库返回的原始节点时间为准，不使用激活时钟。
        request=request.model_copy(update={'created_at':restored.node.created_at})
        # 原生摘要生成保存在新任务的私有记录中；摘要节点只有业务摘要，不混入工程数据。
        try:
            if on_registered is not None:await on_registered(restored,summary_id)
            for event in prepared.generations:
                journal.generations.append(event)
                await journal._save_generation(event)
            observer=(self.output_observer_factory(publish_stream, output_kinds=core.registry.output_kinds())
                      if publish_stream is not None else None)
            result=await core.run(request,journal=journal,publish=publish,publish_progress=publish_progress,output_observer=observer)
        except BaseException as error:
            if journal.status=='generating':
                try:
                    async with asyncio.timeout(core.options.cleanup_timeout_seconds):
                        await journal.abort('cancelled' if isinstance(error,asyncio.CancelledError) else 'failed')
                except Exception:
                    pass
            raise
        return TreeAgentResult(tree_id=tree_id,node_id=node_id,summary_id=summary_id,result=result)
