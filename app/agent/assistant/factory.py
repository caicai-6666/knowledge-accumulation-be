"""个人助理基于 agent-core 的工具与提示词统一装配入口；零 app.agent.core / app.infrastructure.mllm 依赖。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from agent_core import (
    CachePool,
    FileSearchSettings,
    PDFDocument,
    PDFRenderConfig,
    RegisteredTool,
)
from agent_core.core.subgraph.file_retrieval.pool import FileSearchResultSet
from agent_core.core.subgraph.memory_retrieval.pool import (
    DEFAULT_MEMORY_CACHE_POOL,
    MemoryQueryEntry,
)

from app.agent.exploration.tools import (
    LocalFileAuthorizer,
    LocalFileNameResolver,
    build_exploration_local_tools,
)
from app.auth.model import AuthenticatedUser

from .prompt import (
    ASSISTANT_OUTPUT_GUIDANCE,
    build_assistant_prompt_options,
    build_assistant_role_prompt,
)
from .tool import (
    AssistantFinishTask,
    AssistantQueryViewer,
    CalendarQueryViewer,
    FileNoteQueryPool,
    FileNoteQueryTools,
    FileNoteTools,
    FileVisibilityTools,
    PortraitMutationTools,
    PortraitQueryViewer,
    build_assistant_query_pool,
    build_calendar_query_pool,
    build_file_disclosure_registration,
    build_memory_window_registration,
    build_portrait_query_pool,
    build_search_assistants_registration,
    build_search_calendar_events_registration,
    build_search_portraits_registration,
    build_view_assistant_query_registration,
    build_view_calendar_query_registration,
    build_view_portrait_query_registration,
    send_registration,
    wrap_image_authorize,
)
from .tool.finish_task import FinalDeliverCallback
from .tool.send_assistant_message import DeliveryRecorder

# 1B 方案：工作区、压缩、记忆与定时提醒全部使用 agent-core 内置工具名。
ASSISTANT_BUILTIN_TOOLS: tuple[str, ...] = (
    'workspace_add',
    'workspace_replace',
    'workspace_delete',
    'compact_context',
    'search_memory',
    'view_memory_query',
    'create_reminder',
    'list_reminders',
    'delete_reminder',
)


def build_assistant_tools(
    *,
    user: AuthenticatedUser,
    tree_id: str,
    node_id: str | Callable[[], str | None] | None = None,
    # 定制 finish_task（含钉钉转换钩子与统一终态交付回调）
    finish_hooks: Sequence[Any] = (),
    validate_files: Callable[[list[str]], Awaitable[None]] | None = None,
    task_type: str | None = None,
    validate_pruning: Callable[[], Awaitable[dict[str, str] | None]] | None = None,
    on_final_deliver: FinalDeliverCallback | None = None,
    # Phase 1 复用：MCP 工具与本地通用工具
    mcp_tools: Sequence[RegisteredTool] = (),
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
    # 个人助理专属依赖（均按需装配）
    file_visibility_service: Any | None = None,
    file_note_repository: Any | None = None,
    file_disclosure_source: Any | None = None,
    portrait_repository: Any | None = None,
    employee_directory: Any | None = None,
    recipient_exists: Callable[[str], Awaitable[bool]] | None = None,
    record_assistant_delivery: DeliveryRecorder | None = None,
    flush_assistant_delivery: Callable[[], Awaitable[None]] | None = None,
    calendar_service: Any | None = None,
    memory_window_lookup: Callable[..., Awaitable[Sequence[str]]] | None = None,
    memory_cache_pool: CachePool[MemoryQueryEntry] = DEFAULT_MEMORY_CACHE_POOL,
    memory_window_page_size: int = 3,
) -> list[RegisteredTool]:
    """装配个人助理全部工具列表（不含 `ASSISTANT_BUILTIN_TOOLS` 字符串名）。

    组合顺序：
    1. 定制 `finish_task`（兼容 `supports_heartbeat_pruning`，挂载 `finish_hooks` 与 `on_final_deliver`）；
    2. Phase 1 复用本地工具（`view_pdf` 自动叠加 `FileVisibilityService` 的 `public` 门禁）；
    3. 外部传入的 MCP 工具（如 `build_mcp_exploration_tools(...)` 的返回）；
    4. 助理专属工具（文件可见范围、文件备忘、告知检查、画像、员工目录与跨助理消息、钉钉日程、相邻记忆窗口）。
    """
    validated_user = AuthenticatedUser.model_validate(user.model_dump())
    user_id = validated_user.userid

    tools: list[RegisteredTool] = [
        AssistantFinishTask(
            user=validated_user,
            hooks=tuple(finish_hooks),
            validate_files=validate_files,
            task_type=task_type,
            validate_pruning=validate_pruning,
            on_final_deliver=on_final_deliver,
        ).build_registration()
    ]

    # 助理侧本地附件阅读必须叠加 public 可见性门禁；仅传本地附件时强制包装。
    guarded_local_authorize = authorize_local_file
    if authorize_local_file is not None and file_visibility_service is not None:
        guarded_local_authorize = wrap_image_authorize(
            authorize_local_file,
            userid=user_id,
            visibility=file_visibility_service,
        )

    tools.extend(
        build_exploration_local_tools(
            user_id=user_id,
            tree_id=tree_id,
            node_id=node_id,
            file_retrieval_service=file_retrieval_service,
            file_search_platforms=file_search_platforms,
            file_search_settings=file_search_settings,
            file_search_cache_pool=file_search_cache_pool,
            upload_file_store=upload_file_store,
            authorize_local_file=guarded_local_authorize,
            local_file_name=local_file_name,
            registry_file_access=registry_file_access,
            registry_file_client=registry_file_client,
            pdf_cache_pool=pdf_cache_pool,
            pdf_render_config=pdf_render_config,
            platform_directory_service=platform_directory_service,
            platform_a2a_service=platform_a2a_service,
            enable_a2a=enable_a2a,
        )
    )

    tools.extend(mcp_tools)

    if file_visibility_service is not None:
        tools.extend(FileVisibilityTools(file_visibility_service, user_id).registrations())

    if file_note_repository is not None:
        tools.extend(FileNoteTools(file_note_repository, user_id).registrations())
        note_pool = FileNoteQueryPool()
        tools.extend(FileNoteQueryTools(file_note_repository, user_id, pool=note_pool).registrations())

    if file_disclosure_source is not None:
        tools.append(
            build_file_disclosure_registration(
                source=file_disclosure_source,
                owner_userid=user_id,
            )
        )

    if portrait_repository is not None:
        portrait_pool = build_portrait_query_pool(user_id)
        portrait_viewer = PortraitQueryViewer(portrait_pool)
        tools.append(
            build_search_portraits_registration(
                pool=portrait_pool,
                repository=portrait_repository,
                owner_userid=user_id,
            )
        )
        tools.append(build_view_portrait_query_registration(portrait_viewer))
        tools.extend(PortraitMutationTools(portrait_repository, user_id).registrations())

    if employee_directory is not None:
        assistant_pool = build_assistant_query_pool()
        assistant_viewer = AssistantQueryViewer(assistant_pool)
        tools.append(
            build_search_assistants_registration(
                pool=assistant_pool,
                employees=employee_directory,
            )
        )
        tools.append(build_view_assistant_query_registration(assistant_viewer))

    if recipient_exists is not None:
        tools.append(
            send_registration(
                recipient_exists=recipient_exists,
                flush=flush_assistant_delivery,
                record_delivery=record_assistant_delivery,
            )
        )

    if calendar_service is not None:
        calendar_pool = build_calendar_query_pool()
        calendar_viewer = CalendarQueryViewer(calendar_pool)
        tools.append(
            build_search_calendar_events_registration(
                pool=calendar_pool,
                service=calendar_service,
            )
        )
        tools.append(build_view_calendar_query_registration(calendar_viewer))

    if memory_window_lookup is not None:
        tools.append(
            build_memory_window_registration(
                lookup=memory_window_lookup,
                cache_pool=memory_cache_pool,
                page_size=memory_window_page_size,
            )
        )

    return tools


__all__ = [
    'ASSISTANT_BUILTIN_TOOLS',
    'ASSISTANT_OUTPUT_GUIDANCE',
    'build_assistant_prompt_options',
    'build_assistant_role_prompt',
    'build_assistant_tools',
]
