"""探索模式双源 PDF 查看工具；按可信身份路由本地上传附件与注册中心远程 PDF。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
import re
from typing import Any
from uuid import UUID

from agent_core import (
    CachePool,
    PDFDocument,
    PDFRenderConfig,
    PDFViewer,
    RegisteredTool,
    build_pdf_view_registration,
    create_pdf_cache_pool,
)
from agent_core.core.file.pdf_viewer import PDFViewError

from app.infrastructure.registry_file import RegistryFileError, validate_userid
from app.model.question_file import AttachmentReference, UUID_PATTERN

# 注册中心正式资源 ID 为 1..uint64_max 的十进制字符串，与本地上传 UUID 完全不重叠。
_REGISTRY_ID_PATTERN = re.compile(r'^[1-9][0-9]{0,19}$')
_LOCAL_UUID_PATTERN = re.compile(UUID_PATTERN)
_UINT64_MAX = 18446744073709551615

LocalFileAuthorizer = Callable[[str], Any]
LocalFileNameResolver = Callable[[str], str | None | Awaitable[str | None]]

_EXPLORATION_PDF_DESCRIPTION = (
    '当你需要阅读 PDF 文件的原始页面、核对具体内容或继续翻页时，使用这个工具。'
    '支持读取当前会话已上传的本地 PDF 附件（传入附件 UUID）以及用户有权访问的注册中心 PDF 文件'
    '（传入文件查询候选列表中的正式资源 ID）。一次返回一页原生图像及当前页、总页数，帮助依据实际页面作答。'
    '将上下文或前置工具返回的完整文件标识原样填入 file_id；每次查看均按当前用户权限独立校验。'
    '可以指定页码，也可以省略以继续查看下一页；查看失败时会说明原因，阅读位置保持不变。'
    '需要后续继续使用的事实、来源和限制应保存到工作区；需要核对细节时重新查看，不能仅凭查看记录推断原文。'
)


async def _maybe_await(value: Any) -> Any:
    return await value if isawaitable(value) else value


def _is_registry_file_id(file_id: str) -> bool:
    return (
        isinstance(file_id, str)
        and _REGISTRY_ID_PATTERN.fullmatch(file_id) is not None
        and int(file_id) <= _UINT64_MAX
    )


def _is_local_uuid(file_id: str) -> bool:
    if not isinstance(file_id, str) or _LOCAL_UUID_PATTERN.fullmatch(file_id) is None:
        return False
    try:
        UUID(file_id)
    except ValueError:
        return False
    return True


def _extract_local_reference(file_id: str, decision: Any) -> AttachmentReference:
    if isinstance(decision, AttachmentReference):
        if decision.type != 'images':
            raise PDFViewError(
                'unsupported_file',
                '此工具仅支持查看 PDF（images）类型的附件，不支持音频或视频文件。',
                '请核对附件类型并选择 PDF 文件标识后重试。',
            )
        return decision
    # UploadFileStore 中 PDF 附件的语义类型为 'images'（对应 .pdf 磁盘文件）。
    return AttachmentReference(type='images', file_id=file_id)


def _extract_local_name(decision: Any) -> str | None:
    if decision is None or isinstance(decision, bool):
        return None
    for attr in ('name', 'file_name', 'original_name'):
        value = getattr(decision, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(decision, dict):
        for key in ('name', 'file_name', 'original_name'):
            value = decision.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def create_exploration_pdf_viewer(
    *,
    user_id: str,
    upload_file_store: Any | None = None,
    authorize_local_file: LocalFileAuthorizer | None = None,
    local_file_name: LocalFileNameResolver | None = None,
    registry_file_access: Any | None = None,
    registry_file_client: Any | None = None,
    pool: CachePool[PDFDocument] | None = None,
    config: PDFRenderConfig | None = None,
) -> PDFViewer:
    """构造支持本地上传附件 + 注册中心远程 PDF 双源路由的 `PDFViewer`。

    每次 `view` 与 `resolve_page` 均重新触发 `authorize`，确保权限撤销或树归属变化立即生效。
    """
    trusted_user_id = validate_userid(user_id)
    has_local = upload_file_store is not None and authorize_local_file is not None
    has_registry = registry_file_access is not None and registry_file_client is not None
    if not has_local and not has_registry:
        raise ValueError('至少需要配置本地上传附件或注册中心 PDF 来源之一')

    async def authorize(file_id: str) -> bool:
        # 源 B：注册中心正式资源 ID（纯十进制正整数字符串）
        if _is_registry_file_id(file_id):
            if not has_registry:
                return False
            try:
                await registry_file_access.authorize(file_id, userid=trusted_user_id)
                return True
            except RegistryFileError as error:
                if error.code in ('file_forbidden', 'file_not_found'):
                    return False
                raise PDFViewError(
                    error.code,
                    str(error),
                    '注册中心权限校验暂不可用，请稍后重试；不要据此推断文件不存在。',
                    failure_mode='failed',
                ) from None

        # 源 A：本地上传附件（UUID，须经回调校验属于当前用户与当前树）
        if has_local and _is_local_uuid(file_id):
            decision = await _maybe_await(authorize_local_file(file_id))
            return bool(decision)

        return False

    async def load_pdf(file_id: str) -> bytes:
        # 源 B：注册中心远程 PDF，按可信 userid 鉴权后下载原件字节
        if _is_registry_file_id(file_id):
            if not has_registry:
                raise PDFViewError(
                    'file_unavailable',
                    '当前未启用注册中心文件读取能力。',
                    '请核对文件标识，或改用当前会话已上传的附件标识。',
                )
            try:
                return await registry_file_client.read_file(file_id, userid=trusted_user_id)
            except RegistryFileError as error:
                if error.code in ('file_forbidden', 'file_not_found', 'unsupported_file', 'invalid_file', 'file_too_large'):
                    raise PDFViewError(
                        error.code,
                        str(error),
                        '请核对文件标识、文件类型或访问权限后重新选择可读取的 PDF 文件。',
                    ) from None
                raise PDFViewError(
                    error.code,
                    str(error),
                    '注册中心文件服务暂时不可用，请稍后重试；不要据此推断文件内容。',
                    failure_mode='failed',
                ) from None

        # 源 A：本地上传附件，再次核验归属后从 UploadFileStore 读取字节
        if has_local and _is_local_uuid(file_id):
            decision = await _maybe_await(authorize_local_file(file_id))
            if not decision:
                raise PDFViewError(
                    'file_unavailable',
                    '目标 PDF 文件不存在或当前无权访问。',
                    '请核对文件标识及访问权限后重新选择可读取的文件。',
                )
            reference = _extract_local_reference(file_id, decision)
            try:
                return await upload_file_store.read(reference)
            except FileNotFoundError:
                raise PDFViewError(
                    'file_not_found',
                    '未在本地附件存储中找到该 PDF 文件。',
                    '请核对附件标识是否正确，或重新上传文件后再查看。',
                ) from None
            except ValueError as error:
                raise PDFViewError(
                    'invalid_file',
                    str(error),
                    '请确认该附件为有效的 PDF 文件后重试。',
                ) from None

        raise PDFViewError(
            'file_unavailable',
            '目标 PDF 文件不存在或当前无权访问。',
            '请核对文件标识及访问权限后重新选择可读取的文件。',
        )

    async def file_name(file_id: str) -> str | None:
        if _is_registry_file_id(file_id):
            if not has_registry:
                return None
            row = await registry_file_access.authorize(file_id, userid=trusted_user_id)
            name = getattr(row, 'name', None)
            return name.strip() if isinstance(name, str) and name.strip() else None

        if has_local and _is_local_uuid(file_id):
            decision = await _maybe_await(authorize_local_file(file_id))
            if not decision:
                return None
            if local_file_name is not None:
                resolved = await _maybe_await(local_file_name(file_id))
                if isinstance(resolved, str) and resolved.strip():
                    return resolved.strip()
            return _extract_local_name(decision)

        return None

    cache_pool = (
        pool
        if pool is not None
        else create_pdf_cache_pool(name='exploration_pdf_documents', capacity=4, scope='session')
    )
    return PDFViewer(
        load_pdf=load_pdf,
        pool=cache_pool,
        authorize=authorize,
        file_name=file_name,
        config=config or PDFRenderConfig(),
    )


def build_exploration_pdf_tool(
    *,
    user_id: str,
    upload_file_store: Any | None = None,
    authorize_local_file: LocalFileAuthorizer | None = None,
    local_file_name: LocalFileNameResolver | None = None,
    registry_file_access: Any | None = None,
    registry_file_client: Any | None = None,
    pool: CachePool[PDFDocument] | None = None,
    config: PDFRenderConfig | None = None,
    name: str = 'view_pdf',
    description: str | None = None,
    title: str = 'PDF 文件查看',
) -> RegisteredTool:
    """构建探索模式统一的 `view_pdf` 工具，支持本地上传附件与注册中心远程 PDF 双源查看。"""
    viewer = create_exploration_pdf_viewer(
        user_id=user_id,
        upload_file_store=upload_file_store,
        authorize_local_file=authorize_local_file,
        local_file_name=local_file_name,
        registry_file_access=registry_file_access,
        registry_file_client=registry_file_client,
        pool=pool,
        config=config,
    )
    return build_pdf_view_registration(
        viewer,
        name=name,
        description=description or _EXPLORATION_PDF_DESCRIPTION,
        title=title,
    )


__all__ = [
    'LocalFileAuthorizer',
    'LocalFileNameResolver',
    'build_exploration_pdf_tool',
    'create_exploration_pdf_viewer',
]
