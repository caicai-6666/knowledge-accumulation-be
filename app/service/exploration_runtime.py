"""基于 agent-core 的探索模式运行时装配。

职责：
1. 封装 `agent_core.AgentRuntime` 与 `Models(VLLMClient, VLLMEmbedding, vllm_counter)`，复用现有 `.env` 的
   `VLLM_MLLM_*` 与 `VLLM_EMBEDDING_*` 配置；也支持注入测试用 `Models`。
2. 按树创建或恢复 `TreeAgent`（固定 `execution_mode='interruptible'`、`workspace_retention='per_node'`、
   `role=build_exploration_role_prompt()`、`prompt_options=build_exploration_prompt_options()`、
   `guidance_factory=build_exploration_guidance_factory()`）。
3. 统一装配完整工具清单：
   - `agent-core` 内置工具（`workspace_add`, `workspace_replace`, `workspace_delete`, `compact_context`,
     `emit_progress`, `finish_task`, `search_memory`, `view_memory_query`）
   - 本地探索工具（`search_files`, `view_file_candidates`, `view_pdf`, `list_sub_management_platforms`, 可选 A2A）
   - MCP 探索工具（7 个外部工具）
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import logging
from typing import Any, Literal

from agent_core import AgentRuntime, Models, RuntimeSettings, TreeAgent, TreeView, VLLMClient, VLLMEmbedding
from agent_core.config.mllm import MLLMSettings as AgentCoreMLLMSettings
from agent_core.providers import vllm_counter
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.agent.exploration.mcp_tools import build_mcp_exploration_tools
from app.agent.exploration.tools import (
    build_exploration_guidance_factory,
    build_exploration_local_tools,
    build_exploration_prompt_options,
    build_exploration_role_prompt,
    build_exploration_supplemental,
)
from app.auth.model import AuthenticatedUser
from app.config.embedding import EmbeddingSettings, load_optional_embedding_settings
from app.config.mllm import MLLMSettings, load_mllm_settings
from app.infrastructure.mcp_client import MCPClientManager
from app.infrastructure.upload_file import UploadFileStore
from app.repository.exploration_tree import ExplorationTreeRepository

logger = logging.getLogger(__name__)

EXPLORATION_BUILTIN_TOOLS: tuple[str, ...] = (
    'workspace_add',
    'workspace_replace',
    'workspace_delete',
    'compact_context',
    'emit_progress',
    'finish_task',
    'search_memory',
    'view_memory_query',
)


class _DisabledEmbeddingProvider:
    """未配置 Embedding 服务时的占位实现：仅在真正触发向量化时返回明确错误。"""

    space_id: str = 'disabled'
    dimensions: int = 1

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: Literal['query', 'document'],
    ) -> list[list[float]]:
        raise RuntimeError('未配置 VLLM_EMBEDDING_BASE_URL，向量检索不可用')


def _to_agent_core_mllm_settings(settings: MLLMSettings) -> AgentCoreMLLMSettings:
    payload = settings.model_dump()
    payload['api_key'] = SecretStr(settings.api_key.get_secret_value())
    return AgentCoreMLLMSettings.model_validate(payload)


def build_default_models(
    *,
    mllm_settings: MLLMSettings | None = None,
    embedding_settings: EmbeddingSettings | None = None,
) -> tuple[Models, list[Callable[[], Any]]]:
    """按 `.env` 现有配置构建 `agent_core.Models` 及对应的异步清理回调。"""
    mllm = mllm_settings if mllm_settings is not None else load_mllm_settings()
    core_mllm = _to_agent_core_mllm_settings(mllm)
    chat_client = VLLMClient(core_mllm)
    cleanups: list[Callable[[], Any]] = [chat_client.close]

    emb_cfg = embedding_settings if embedding_settings is not None else load_optional_embedding_settings()
    if emb_cfg is not None:
        openai_client = AsyncOpenAI(
            base_url=str(emb_cfg.base_url),
            api_key=emb_cfg.api_key.get_secret_value() or 'EMPTY',
            timeout=emb_cfg.timeout_seconds,
        )
        cleanups.append(openai_client.close)
        embedding_provider: Any = VLLMEmbedding(
            client=openai_client,
            model=emb_cfg.model,
            space_id=f'vllm:{emb_cfg.model}:{emb_cfg.dimensions}',
            dimensions=emb_cfg.dimensions,
        )
    else:
        embedding_provider = _DisabledEmbeddingProvider()

    models = Models(
        daily=chat_client,
        embedding=embedding_provider,
        count_tokens=vllm_counter(core_mllm),
    )
    return models, cleanups


def build_default_runtime_settings(*, mllm_settings: MLLMSettings | None = None) -> RuntimeSettings:
    """按 .env 的 VLLM_MLLM_* 配置生成 agent-core 运行设置，避免退回 32k 默认窗口。"""
    mllm = mllm_settings if mllm_settings is not None else load_mllm_settings()
    core_mllm = _to_agent_core_mllm_settings(mllm)
    return RuntimeSettings(context_window_tokens=core_mllm.context_window_tokens, generation=core_mllm.generation)


class ExplorationRuntimeService:
    """管理探索模式 `AgentRuntime`、每棵树的 `TreeAgent` 驻留及工具装配。"""

    def __init__(
        self,
        repository: ExplorationTreeRepository,
        *,
        models: Models | None = None,
        models_factory: Callable[[], tuple[Models, list[Callable[[], Any]]]] | None = None,
        runtime_settings: RuntimeSettings | None = None,
        upload_files: UploadFileStore | None = None,
        mcp_client: MCPClientManager | None = None,
        file_retrieval_service: Any = None,
        registry_file_access: Any = None,
        registry_file_client: Any = None,
        platform_directory_service: Any = None,
        platform_a2a_service: Any = None,
        enable_a2a: bool = False,
        extra_tools: Sequence[Any] = (),
        progress_reminder_interval_seconds: float | None = 60.0,
        capacity: int = 100,
        ttl_seconds: int = 1800,
    ) -> None:
        self._repository = repository
        self._injected_models = models
        self._models_factory = models_factory or build_default_models
        self._runtime_settings = runtime_settings
        self._upload_files = upload_files if upload_files is not None else UploadFileStore()
        self._mcp_client = mcp_client
        self._file_retrieval_service = file_retrieval_service
        self._registry_file_access = registry_file_access
        self._registry_file_client = registry_file_client
        self._platform_directory_service = platform_directory_service
        self._platform_a2a_service = platform_a2a_service
        self._enable_a2a = enable_a2a
        self._extra_tools = tuple(extra_tools)
        self._progress_reminder_interval_seconds = progress_reminder_interval_seconds
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds

        self._runtime: AgentRuntime | None = None
        self._model_cleanups: list[Callable[[], Any]] = []
        self._agents: dict[str, TreeAgent] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def bind_platform_services(
        self,
        *,
        platform_directory_service: Any = None,
        registry_file_access: Any = None,
        platform_a2a_service: Any = None,
        enable_a2a: bool | None = None,
    ) -> None:
        """允许 lifespan 在中间件启动后补绑平台目录、远程文件访问与 A2A 服务。"""
        if platform_directory_service is not None:
            self._platform_directory_service = platform_directory_service
        if registry_file_access is not None:
            self._registry_file_access = registry_file_access
        if platform_a2a_service is not None:
            self._platform_a2a_service = platform_a2a_service
        if enable_a2a is not None:
            self._enable_a2a = enable_a2a

    async def _ensure_runtime(self) -> AgentRuntime:
        if self._closed:
            raise RuntimeError('探索运行时已关闭')
        if self._runtime is not None:
            return self._runtime
        async with self._lock:
            if self._closed:
                raise RuntimeError('探索运行时已关闭')
            if self._runtime is not None:
                return self._runtime
            storage = await self._repository.ensure_connected()
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

    async def _build_tools(self, tree_id: str, user: AuthenticatedUser) -> list[Any]:
        async def authorize_local_file(file_id: str) -> bool:
            return await self._repository.has_file(tree_id, user.userid, file_id)

        async def local_file_name(file_id: str) -> str | None:
            return await self._repository.get_file_name(tree_id, user.userid, file_id)

        local_tools = build_exploration_local_tools(
            user_id=user.userid,
            tree_id=tree_id,
            file_retrieval_service=self._file_retrieval_service,
            upload_file_store=self._upload_files,
            authorize_local_file=authorize_local_file,
            local_file_name=local_file_name,
            registry_file_access=self._registry_file_access,
            registry_file_client=self._registry_file_client,
            platform_directory_service=self._platform_directory_service,
            platform_a2a_service=self._platform_a2a_service,
            enable_a2a=self._enable_a2a,
        )

        mcp_tools: list[Any] = []
        if self._mcp_client is not None:
            mcp_tools = await build_mcp_exploration_tools(self._mcp_client)

        return [*EXPLORATION_BUILTIN_TOOLS, *local_tools, *mcp_tools, *self._extra_tools]

    @staticmethod
    def build_supplemental(user: AuthenticatedUser, *, extra: str = '') -> str:
        return build_exploration_supplemental(user, extra=extra)

    async def get_agent(self, tree_id: str, *, user: AuthenticatedUser) -> TreeAgent:
        """获取或恢复指定探索树的驻留 TreeAgent（interruptible + per_node）。"""
        if self._closed:
            raise RuntimeError('探索运行时已关闭')
        existing = self._agents.get(tree_id)
        if existing is not None:
            return existing
        runtime = await self._ensure_runtime()
        storage = await self._repository.ensure_connected()
        tools = await self._build_tools(tree_id, user)
        role = build_exploration_role_prompt()
        prompt_options = build_exploration_prompt_options()
        guidance_factory = build_exploration_guidance_factory(
            interval_seconds=self._progress_reminder_interval_seconds,
        )
        async with self._lock:
            existing = self._agents.get(tree_id)
            if existing is not None:
                return existing
            try:
                agent = await runtime.restore(
                    tree_id=tree_id,
                    role=role,
                    database=storage,
                    tools=tools,
                    persist_new_nodes=True,
                    execution_mode='interruptible',
                    guidance_factory=guidance_factory,
                    prompt_options=prompt_options,
                )
            except KeyError:
                agent = await runtime.create(
                    tree_id=tree_id,
                    role=role,
                    database=storage,
                    tools=tools,
                    persist_new_nodes=True,
                    execution_mode='interruptible',
                    workspace_retention='per_node',
                    guidance_factory=guidance_factory,
                    prompt_options=prompt_options,
                )
            self._agents[tree_id] = agent
            return agent

    async def try_tree_view(self, tree_id: str, *, include_display: bool = True) -> TreeView | None:
        """只读获取整棵任务树视图；空树（尚无 agent-core 记录）返回 None。"""
        if self._closed:
            raise RuntimeError('探索运行时已关闭')
        storage = await self._repository.ensure_connected()
        # 若 AgentRuntime 已初始化则复用；否则仅读存储时临时用轻量读取，避免空树 list/open 强依赖 MLLM 配置。
        if self._runtime is not None:
            try:
                return await self._runtime.tree(tree_id, database=storage, include_display=include_display)
            except KeyError:
                return None
        from agent_core.tasks import TaskRepository

        repo = TaskRepository(storage, owner='agent-core:trees', tree_id=tree_id, workspace_retention='per_node')
        tree_record = await storage.read(repo.scope, 'tree')
        if tree_record is None:
            return None
        return await repo.tree(include_display=include_display)

    async def close_tree(self, tree_id: str) -> None:
        async with self._lock:
            agent = self._agents.pop(tree_id, None)
        if agent is not None:
            await agent.close()

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
                result = cleanup()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
