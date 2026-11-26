"""钉钉附件格式路由：经由 MCP `file_conversion` 统一转 PDF 并做轻量 PyMuPDF 可打开校验。"""
from __future__ import annotations

import asyncio
from pathlib import PurePath
from typing import Any

import pymupdf

from agent_core.infrastructure.pdf_lock import serialized_pdf_operation

from app.infrastructure.mcp_client import ConversionError, MCPClientManager, MCPUnavailableError

IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif',
    '.tiff', '.avif', '.heic', '.heif', '.heics', '.heifs', '.hif',
})
SUPPORTED_FILE_EXTENSIONS = IMAGE_EXTENSIONS | {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
}
_UNAVAILABLE_CONVERSION_CODES = frozenset({
    'converter_unavailable',
    'font_unavailable',
    'converter_busy',
    'conversion_timeout',
})


class AttachmentRejected(ValueError):
    """文件内容问题，可向用户说明，不回显底层异常。"""


class AttachmentConversionUnavailable(RuntimeError):
    """转换依赖暂不可用；保留入站记录重试，不拒收为坏文件。"""


def supported(part: Any) -> bool:
    """判断钉钉入站附件片段是否在允许的文件/图片范围内。"""
    return (
        part.type == 'picture'
        or (
            part.type == 'file'
            and PurePath(part.filename).suffix.lower() in SUPPORTED_FILE_EXTENSIONS
        )
    )


@serialized_pdf_operation
def validate_pdf(content: bytes) -> bytes:
    """轻量 PyMuPDF 可打开性校验；复用 agent-core 全局 PDF 互斥锁。"""
    try:
        if not isinstance(content, bytes) or not content.startswith(b'%PDF-'):
            raise ValueError()
        with pymupdf.open(stream=content, filetype='pdf') as document:
            if not document.is_pdf or document.needs_pass or not document.page_count:
                raise ValueError()
            for page in document:
                if page.rect.is_empty or page.rect.is_infinite:
                    raise ValueError()
    except (ValueError, RuntimeError):
        raise AttachmentRejected('PDF 损坏、加密或没有有效页面') from None
    return content


async def to_pdf_async(
    part: Any,
    content: bytes,
    mcp_client: MCPClientManager | None,
    *,
    max_bytes: int,
) -> bytes:
    """异步调用 MCP `file_conversion` 将钉钉附件转为 PDF，并执行大小与可打开性校验。"""
    if not supported(part):
        raise AttachmentRejected('暂不支持该文件类型，请提供图片、PDF、DOC/DOCX、XLS/XLSX 或 PPT/PPTX')
    if mcp_client is None:
        raise AttachmentConversionUnavailable('文档转换服务未装配，请稍后重试')

    filename = part.filename if getattr(part, 'type', None) == 'file' and getattr(part, 'filename', None) else None
    try:
        result = await mcp_client.convert_file(content, filename=filename, operation='auto')
    except MCPUnavailableError:
        raise AttachmentConversionUnavailable('文档转换服务暂不可用，请稍后重试') from None
    except ConversionError as error:
        if error.code in _UNAVAILABLE_CONVERSION_CODES:
            raise AttachmentConversionUnavailable(error.reason) from None
        raise AttachmentRejected(error.reason) from None

    if not result.content or len(result.content) > max_bytes:
        raise AttachmentRejected('转换后的 PDF 大小超出限制')
    await asyncio.to_thread(validate_pdf, result.content)
    return result.content
