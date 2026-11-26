"""探索模式文件检索后端与工具装配；桥接注册中心 FileRetrievalService 到 agent-core。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agent_core import (
    CachePool,
    FileSearchBackend,
    FileSearchRecord,
    FileSearchSettings,
    RegisteredTool,
    build_file_candidates_registration,
    build_file_search_registration,
)
from agent_core.core.subgraph.file_retrieval.backend import FileSearchField
from agent_core.core.subgraph.file_retrieval.pool import FileSearchResultSet

from app.infrastructure.registry_file import validate_userid

# agent-core 通用检索字段到注册中心 FileRetrievalService 字段的显式映射。
_FIELD_MAPPING: dict[FileSearchField, str] = {
    'name': 'name',
    'summary': 'abstract',
    'tag': 'passport',
}


def _resolve_file_search_settings(
    service: Any,
    override: FileSearchSettings | None = None,
) -> FileSearchSettings:
    """优先使用显式覆盖配置；否则尽量继承 FileRetrievalService 的已有检索参数。"""
    if override is not None:
        return override
    svc_settings = getattr(service, 'settings', None)
    if svc_settings is None:
        return FileSearchSettings()
    defaults = FileSearchSettings()
    return FileSearchSettings(
        cache_max_queries=getattr(svc_settings, 'cache_max_queries', defaults.cache_max_queries),
        rrf_k=getattr(svc_settings, 'rrf_k', defaults.rrf_k),
        history_weight=getattr(svc_settings, 'history_weight', defaults.history_weight),
        max_rounds=getattr(svc_settings, 'max_rounds', defaults.max_rounds),
        page_size=getattr(svc_settings, 'page_size', defaults.page_size),
    )


class RegistryFileSearchBackend:
    """将注册中心 FileRetrievalService 适配为 agent-core 的 FileSearchBackend 协议。

    可信 `user_id` 与可选平台白名单由后端构造时固定绑定，不从模型参数读取。
    """

    def __init__(
        self,
        service: Any,
        *,
        user_id: str,
        platforms: Sequence[str] | None = None,
        settings: FileSearchSettings | None = None,
    ) -> None:
        if service is None:
            raise ValueError('file_retrieval_service 不能为空')
        self._service = service
        self._user_id = validate_userid(user_id)
        self._platforms: tuple[str, ...] | None = (
            tuple(platforms) if platforms is not None else None
        )
        self._settings = _resolve_file_search_settings(service, settings)

    @property
    def settings(self) -> FileSearchSettings:
        return self._settings

    async def read(
        self,
        file_ids: Sequence[str] | None = None,
    ) -> Sequence[FileSearchRecord]:
        if file_ids is not None and not file_ids:
            return ()
        ids_param = tuple(file_ids) if file_ids is not None else None
        rows = await self._service.read(
            self._user_id,
            ids=ids_param,
            platforms=self._platforms,
        )
        records: list[FileSearchRecord] = []
        for row in rows:
            # 保留来源平台与来源平台文件 ID，供多平台 A2A 协作按规范引用。
            metadata: dict[str, str] = {
                '来源平台代码': str(row.platform),
                '文件权限等级': str(row.level),
            }
            if getattr(row, 'source_id', None):
                metadata['来源平台文件 ID'] = str(row.source_id)
            tags = tuple(row.passport) if getattr(row, 'passport', None) else ()
            records.append(
                FileSearchRecord(
                    file_id=str(row.id),
                    name=row.name or '',
                    summary=row.abstract,
                    tags=tags,
                    metadata=metadata,
                )
            )
        return tuple(records)

    async def search(
        self,
        field: FileSearchField,
        query: str,
        scope: Sequence[str] | None = None,
    ) -> Sequence[tuple[str, float]]:
        mapped_field = _FIELD_MAPPING.get(field)
        if mapped_field is None:
            raise ValueError(f'不支持的文件检索字段：{field}')
        if scope is not None and not scope:
            return ()
        ids_param = tuple(scope) if scope is not None else None
        results = await self._service.search(
            self._user_id,
            mapped_field,
            query,
            ids=ids_param,
            platforms=self._platforms,
        )
        return tuple((str(file_id), float(score)) for file_id, score in results)


def build_exploration_file_search_tools(
    file_retrieval_service: Any,
    *,
    user_id: str,
    platforms: Sequence[str] | None = None,
    settings: FileSearchSettings | None = None,
    cache_pool: CachePool[FileSearchResultSet] | None = None,
) -> list[RegisteredTool]:
    """静态装配探索模式文件检索与候选查看工具（`search_files`、`view_file_candidates`）。

    省略 `core`/`journal`/`thinking_effort`，由 agent-core 在执行期通过
    `current_tool_runtime()` 自动复用主任务的运行时与审计日志。
    """
    backend: FileSearchBackend = RegistryFileSearchBackend(
        file_retrieval_service,
        user_id=user_id,
        platforms=platforms,
        settings=settings,
    )
    if cache_pool is not None:
        search_tool = build_file_search_registration(backend=backend, cache_pool=cache_pool)
        candidates_tool = build_file_candidates_registration(
            backend=backend,
            cache_pool=cache_pool,
        )
    else:
        search_tool = build_file_search_registration(backend=backend)
        candidates_tool = build_file_candidates_registration(backend=backend)
    return [search_tool, candidates_tool]


__all__ = [
    'RegistryFileSearchBackend',
    'build_exploration_file_search_tools',
]
