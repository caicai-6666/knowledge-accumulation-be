"""基于 agent-core 的个人助理运行时与 1B 全局心跳/提醒后台巡检服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import hashlib
import logging
from typing import Any

from agent_core import AgentRuntime, Models, RuntimeSettings, SchedulerSettings, TreeAgent, TreeView
from agent_core.core.runtime import AgentCoreError
from agent_core.core.tool.runtime_context import current_tool_runtime

from app.agent.assistant import (
    ASSISTANT_BUILTIN_TOOLS,
    build_assistant_prompt_options,
    build_assistant_role_prompt,
    build_assistant_tools,
)
from app.agent.exploration.mcp_tools import build_mcp_exploration_tools
from app.auth.model import AuthenticatedUser
from app.infrastructure.mcp_client import MCPClientManager
from app.infrastructure.upload_file import UploadFileStore
from app.model.assistant import Assistant
from app.model.question_tree import new_tree_id
from app.service.exploration_runtime import build_default_models, build_default_runtime_settings

logger = logging.getLogger(__name__)


def _current_task_id(fallback: str | None = None) -> str:
    """读取当前执行中工具对应的 agent-core task_id，回退到调用方提供值或新 UUID。"""
    try:
        runtime_ctx = current_tool_runtime()
    except RuntimeError:
        runtime_ctx = None
    if runtime_ctx is not None:
        journal = getattr(runtime_ctx, 'journal', None)
        task = getattr(journal, 'task', None)
        tid = getattr(task, 'id', None)
        if isinstance(tid, str) and tid:
            return tid
    return fallback or new_tree_id()


class AssistantRuntimeService:
    """封装个人助理 `AgentRuntime` 与按用户 `tree_id` 驻留的 `TreeAgent(execution_mode='queue', workspace_retention='latest')`。"""

    def __init__(
        self,
        storage_provider: Callable[[], Any],
        *,
        models: Models | None = None,
        models_factory: Callable[[], tuple[Models, list[Callable[[], Any]]]] | None = None,
        runtime_settings: RuntimeSettings | None = None,
        scheduler_settings: SchedulerSettings | None = None,
        finish_hooks: Sequence[Any] = (),
        upload_files: UploadFileStore | None = None,
        mcp_client: MCPClientManager | None = None,
        assistant_repository: Any = None,
        delivery_service: Any = None,
        file_delivery_service: Any = None,
        employees: Any = None,
        calendar_service: Any = None,
        file_retrieval_service: Any = None,
        registry_file_access: Any = None,
        registry_file_client: Any = None,
        platform_directory_service: Any = None,
        platform_a2a_service: Any = None,
        enable_a2a: bool = False,
        file_visibility_service: Any = None,
        file_note_repository: Any = None,
        portrait_repository: Any = None,
        capacity: int = 100,
        ttl_seconds: int = 1800,
    ) -> None:
        self._storage_provider = storage_provider
        self._injected_models = models
        self._models_factory = models_factory or build_default_models
        self._runtime_settings = runtime_settings
        self._scheduler_settings = scheduler_settings or SchedulerSettings(heartbeat_interval_seconds=-1)
        self._finish_hooks = tuple(finish_hooks)
        self._upload_files = upload_files if upload_files is not None else UploadFileStore()
        self._mcp_client = mcp_client
        self._assistant_repository = assistant_repository
        self._delivery_service = delivery_service
        self._file_delivery_service = file_delivery_service
        self._employees = employees
        self._calendar_service = calendar_service
        self._file_retrieval_service = file_retrieval_service
        self._registry_file_access = registry_file_access
        self._registry_file_client = registry_file_client
        self._platform_directory_service = platform_directory_service
        self._platform_a2a_service = platform_a2a_service
        self._enable_a2a = enable_a2a
        self._file_visibility_service = file_visibility_service
        self._file_note_repository = file_note_repository
        self._portrait_repository = portrait_repository
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds

        self._runtime: AgentRuntime | None = None
        self._model_cleanups: list[Callable[[], Any]] = []
        self._agents: dict[str, TreeAgent] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def _ensure_storage(self):
        result = self._storage_provider()
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _ensure_runtime(self) -> AgentRuntime:
        if self._closed:
            raise RuntimeError('个人助理运行时已关闭')
        if self._runtime is not None:
            return self._runtime
        async with self._lock:
            if self._closed:
                raise RuntimeError('个人助理运行时已关闭')
            if self._runtime is not None:
                return self._runtime
            storage = await self._ensure_storage()
            if self._injected_models is not None:
                models = self._injected_models
            else:
                models, cleanups = self._models_factory()
                self._model_cleanups.extend(cleanups)
            runtime_settings = self._runtime_settings
            # 仅默认模型工厂路径从 .env 装配 RuntimeSettings，避免退回 32k 默认窗口且不影响注入模型的单测。
            if (
                runtime_settings is None
                and self._injected_models is None
                and self._models_factory is build_default_models
            ):
                runtime_settings = build_default_runtime_settings()
            self._runtime = AgentRuntime(
                models=models,
                database=storage,
                capacity=self._capacity,
                ttl_seconds=self._ttl_seconds,
                settings=runtime_settings,
            )
            return self._runtime

    async def _build_tools(self, assistant: Assistant, user: AuthenticatedUser) -> list[Any]:
        mcp_tools: list[Any] = []
        if self._mcp_client is not None:
            mcp_tools = await build_mcp_exploration_tools(self._mcp_client)

        async def authorize_local_file(file_id: str) -> bool:
            if self._assistant_repository is None:
                return False
            return await self._assistant_repository.has_file(assistant.tree_id, user.userid, file_id)

        validate_files = None
        if self._file_delivery_service is not None:
            async def _validate_files(file_ids: list[str]) -> None:
                await self._file_delivery_service.validate(user.userid, assistant.tree_id, file_ids)
            validate_files = _validate_files

        async def on_final_deliver(task_id: str | None, final_text: str, file_ids: list[str]) -> None:
            if self._assistant_repository is None:
                return
            effective_task_id = _current_task_id(task_id)
            await self._assistant_repository.record_delivery(
                userid=user.userid,
                task_id=effective_task_id,
                action_key='final',
                recipient_userid=user.userid,
                message=final_text,
                destination='user',
            )
            for fid in file_ids:
                await self._assistant_repository.record_delivery(
                    userid=user.userid,
                    task_id=effective_task_id,
                    action_key='file:' + fid,
                    recipient_userid=user.userid,
                    message='用户附件',
                    destination='user',
                    file_id=fid,
                )
            if self._delivery_service is not None:
                await self._delivery_service.flush(effective_task_id)

        async def recipient_exists(target_userid: str) -> bool:
            if target_userid == user.userid:
                return False
            if self._employees is not None and await self._employees.find_by_userid(target_userid) is None:
                return False
            if self._assistant_repository is not None:
                return await self._assistant_repository.recipient_enabled(target_userid)
            return True

        last_delivery_task_id: list[str] = []

        async def record_assistant_delivery(call_id: str, recipient_userid: str, message: str) -> None:
            if self._assistant_repository is None:
                return
            effective_task_id = _current_task_id()
            last_delivery_task_id.clear()
            last_delivery_task_id.append(effective_task_id)
            key = 'call:' + hashlib.sha256(call_id.encode()).hexdigest()
            await self._assistant_repository.record_delivery(
                userid=user.userid,
                task_id=effective_task_id,
                action_key=key,
                recipient_userid=recipient_userid,
                message=message,
                destination='assistant',
            )

        async def flush_assistant_delivery() -> None:
            if self._delivery_service is not None:
                fallback = last_delivery_task_id[0] if last_delivery_task_id else None
                effective_task_id = _current_task_id(fallback)
                await self._delivery_service.flush(effective_task_id)

        custom_tools = build_assistant_tools(
            user=user,
            tree_id=assistant.tree_id,
            finish_hooks=self._finish_hooks,
            validate_files=validate_files,
            on_final_deliver=on_final_deliver,
            mcp_tools=mcp_tools,
            file_retrieval_service=self._file_retrieval_service,
            upload_file_store=self._upload_files,
            authorize_local_file=authorize_local_file,
            registry_file_access=self._registry_file_access,
            registry_file_client=self._registry_file_client,
            platform_directory_service=self._platform_directory_service,
            platform_a2a_service=self._platform_a2a_service,
            enable_a2a=self._enable_a2a,
            file_visibility_service=self._file_visibility_service,
            file_note_repository=self._file_note_repository,
            portrait_repository=self._portrait_repository,
            employee_directory=self._employees,
            recipient_exists=recipient_exists,
            record_assistant_delivery=record_assistant_delivery,
            flush_assistant_delivery=flush_assistant_delivery,
            calendar_service=self._calendar_service,
        )
        return [*ASSISTANT_BUILTIN_TOOLS, *custom_tools]

    async def get_agent(self, assistant: Assistant, *, user: AuthenticatedUser) -> TreeAgent:
        """获取或恢复指定个人助理的驻留 TreeAgent（queue + latest）。"""
        if self._closed:
            raise RuntimeError('个人助理运行时已关闭')
        existing = self._agents.get(assistant.tree_id)
        if existing is not None:
            return existing
        runtime = await self._ensure_runtime()
        storage = await self._ensure_storage()
        tools = await self._build_tools(assistant, user)
        role = build_assistant_role_prompt()
        prompt_options = build_assistant_prompt_options()
        async with self._lock:
            existing = self._agents.get(assistant.tree_id)
            if existing is not None:
                return existing
            try:
                agent = await runtime.restore(
                    tree_id=assistant.tree_id,
                    role=role,
                    database=storage,
                    tools=tools,
                    persist_new_nodes=True,
                    execution_mode='queue',
                    heartbeat=self._scheduler_settings,
                    prompt_options=prompt_options,
                )
            except KeyError:
                agent = await runtime.create(
                    tree_id=assistant.tree_id,
                    role=role,
                    database=storage,
                    tools=tools,
                    persist_new_nodes=True,
                    execution_mode='queue',
                    workspace_retention='latest',
                    heartbeat=self._scheduler_settings,
                    prompt_options=prompt_options,
                )
            self._agents[assistant.tree_id] = agent
            return agent

    async def run_task(
        self,
        assistant: Assistant,
        *,
        user: AuthenticatedUser,
        question: str,
        external_id: str | None = None,
        attachments: Sequence[dict[str, Any]] = (),
        thinking_effort: str = 'medium',
        supplemental: str = '',
        source: str = 'user',
        priority: int = 0,
    ) -> Any:
        agent = await self.get_agent(assistant, user=user)
        job = await agent.generate(
            question,
            external_id=external_id,
            attachments=attachments,
            thinking_effort=thinking_effort,
            supplemental=supplemental,
            source=source,
            priority=priority,
            preview=False,
        )
        return await job.result()

    async def tick_user(self, assistant: Assistant, *, user: AuthenticatedUser) -> Any:
        """驱动指定助理的 agent-core AgentScheduler.tick()（到期提醒 + 空闲心跳）。"""
        agent = await self.get_agent(assistant, user=user)
        sched = await agent.scheduler(settings=self._scheduler_settings)
        return await sched.tick()

    async def try_tree_view(self, tree_id: str) -> TreeView | None:
        if self._closed:
            raise RuntimeError('个人助理运行时已关闭')
        storage = await self._ensure_storage()
        if self._runtime is not None:
            try:
                return await self._runtime.tree(tree_id, database=storage)
            except KeyError:
                return None
        from agent_core.tasks import TaskRepository

        repo = TaskRepository(storage, owner='agent-core:trees', tree_id=tree_id, workspace_retention='latest')
        tree_record = await storage.read(repo.scope, 'tree')
        if tree_record is None:
            return None
        return await repo.tree()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            agents = list(self._agents.values())
            self._agents.clear()
            runtime, self._runtime = self._runtime, None
            cleanups = list(self._model_cleanups)
            self._model_cleanups.clear()
        for agent in agents:
            try:
                await agent.close()
            except Exception:
                pass
        if runtime is not None:
            try:
                await runtime.close()
            except Exception:
                pass
        for cleanup in cleanups:
            try:
                res = cleanup()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass


class AssistantSchedulerRunner:
    """1B 全局后台巡检器：周期遍历已启用助理，驱动 agent-core 原生提醒与空闲心跳。"""

    def __init__(
        self,
        repository: Any,
        employees: Any,
        runtime: AssistantRuntimeService,
        *,
        poll_seconds: float = 15.0,
        enabled: bool = True,
    ) -> None:
        self._repository = repository
        self._employees = employees
        self._runtime = runtime
        self._poll_seconds = poll_seconds
        self._enabled = enabled
        self._task: asyncio.Task | None = None
        self._closed = False

    async def tick_once(self) -> int:
        """执行一轮全量巡检，返回实际触发 tick 的助理数量。"""
        assistants = await self._repository.list_enabled_assistants()
        ticked = 0
        for assistant in assistants:
            if self._closed:
                break
            user = await self._employees.find_by_userid(assistant.userid)
            if user is None or user.employment_status == 'resigned':
                continue
            scope_fn = getattr(self._repository, 'user_scope', None)
            if callable(scope_fn):
                async with scope_fn(assistant.userid) as acquired:
                    if not acquired:
                        continue
                    await self._runtime.tick_user(assistant, user=user)
                    ticked += 1
            else:
                await self._runtime.tick_user(assistant, user=user)
                ticked += 1
        return ticked

    async def _loop(self) -> None:
        while not self._closed:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    '助理调度巡检失败：error_type=%s reason=%s cause_type=%s',
                    type(error).__name__,
                    str(error)[:200] if isinstance(error, AgentCoreError) else '-',
                    type(error.__context__).__name__ if error.__context__ else '-',
                )
            await asyncio.sleep(self._poll_seconds)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError('助理调度巡检器已关闭')
        if self._enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
