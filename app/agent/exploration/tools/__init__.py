"""探索模式本地工具与提示词统一装配入口（基于 agent-core）。"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from agent_core import (
    CachePool,
    FileSearchSettings,
    PDFDocument,
    PDFRenderConfig,
    RegisteredTool,
)
from agent_core.core.subgraph.file_retrieval.pool import FileSearchResultSet

from .file_search import (
    RegistryFileSearchBackend,
    build_exploration_file_search_tools,
)
from .pdf_viewer import (
    LocalFileAuthorizer,
    LocalFileNameResolver,
    build_exploration_pdf_tool,
    create_exploration_pdf_viewer,
)
from .platform_tools import (
    BroadcastPlatformA2AArguments,
    ContinuePlatformA2AArguments,
    ListSubManagementPlatformsArguments,
    SubManagementPlatformViewer,
    build_platform_a2a_tools,
    build_sub_management_platform_tool,
)
from .prompt import (
    EXPLORATION_OPENING_PROMPT,
    EXPLORATION_OUTPUT_GUIDANCE,
    EXPLORATION_REMINDER_PROMPT,
    build_exploration_guidance_factory,
    build_exploration_prompt_options,
    build_exploration_role_prompt,
    build_exploration_supplemental,
)


def build_exploration_local_tools(
    *,
    user_id: str,
    tree_id: str | None = None,
    node_id: str | Callable[[], str | None] | None = None,
    file_retrieval_service: Any | None = None,
    file_search_platforms: Sequence[str] | None = None,
    file_search_settings: FileSearchSettings | None = None,
    file_search_cache_pool: CachePool[FileSearchResultSet] | None = None,
    upload_file_store: Any | None = None,
    authorize_local_file: LocalFileAuthorizer | None = None,
    local_file_name: LocalFileNameResolver | None = None,
    registry_file_access: Any | None = None,
    registry_file_client: Any | None = None,
    pdf_cache_pool: CachePool[PDFDocument] | None = None,
    pdf_render_config: PDFRenderConfig | None = None,
    platform_directory_service: Any | None = None,
    platform_a2a_service: Any | None = None,
    enable_a2a: bool = False,
) -> list[RegisteredTool]:
    """按可用依赖一键装配探索模式全部本地工具。

    装配规则：
    - 配置 `file_retrieval_service` 时，装配 `search_files` 与 `view_file_candidates`；
    - 配置本地附件或注册中心 PDF 来源任一即可装配统一的 `view_pdf`；
    - 配置 `platform_directory_service` 时，装配 `list_sub_management_platforms`；
    - 仅当 `enable_a2a=True` 且提供 `platform_a2a_service` 与 `tree_id` 时，装配
      `broadcast_platform_a2a` 与 `continue_platform_a2a`。
    """
    tools: list[RegisteredTool] = []

    if file_retrieval_service is not None:
        tools.extend(
            build_exploration_file_search_tools(
                file_retrieval_service,
                user_id=user_id,
                platforms=file_search_platforms,
                settings=file_search_settings,
                cache_pool=file_search_cache_pool,
            )
        )

    has_local_pdf = upload_file_store is not None and authorize_local_file is not None
    has_registry_pdf = registry_file_access is not None and registry_file_client is not None
    if has_local_pdf or has_registry_pdf:
        tools.append(
            build_exploration_pdf_tool(
                user_id=user_id,
                upload_file_store=upload_file_store,
                authorize_local_file=authorize_local_file,
                local_file_name=local_file_name,
                registry_file_access=registry_file_access,
                registry_file_client=registry_file_client,
                pool=pdf_cache_pool,
                config=pdf_render_config,
            )
        )

    if platform_directory_service is not None:
        tools.append(
            build_sub_management_platform_tool(
                platform_directory_service,
                user_id=user_id,
            )
        )

    if enable_a2a and platform_a2a_service is not None:
        if not tree_id:
            raise ValueError('启用跨平台 A2A 工具时必须提供 tree_id')
        tools.extend(
            build_platform_a2a_tools(
                platform_a2a_service,
                user_id=user_id,
                tree_id=tree_id,
                node_id=node_id,
            )
        )

    return tools


__all__ = [
    'BroadcastPlatformA2AArguments',
    'ContinuePlatformA2AArguments',
    'EXPLORATION_OPENING_PROMPT',
    'EXPLORATION_OUTPUT_GUIDANCE',
    'EXPLORATION_REMINDER_PROMPT',
    'ListSubManagementPlatformsArguments',
    'LocalFileAuthorizer',
    'LocalFileNameResolver',
    'RegistryFileSearchBackend',
    'SubManagementPlatformViewer',
    'build_exploration_file_search_tools',
    'build_exploration_guidance_factory',
    'build_exploration_local_tools',
    'build_exploration_pdf_tool',
    'build_exploration_prompt_options',
    'build_exploration_role_prompt',
    'build_exploration_supplemental',
    'build_platform_a2a_tools',
    'build_sub_management_platform_tool',
    'create_exploration_pdf_viewer',
]
