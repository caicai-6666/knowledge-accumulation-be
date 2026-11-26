"""探索上传适配：经由 MCP `file_conversion` 统一转 PDF，并执行单份/总计体积校验。"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePath

from app.infrastructure.mcp_client import ConversionError, MCPClientManager
from app.model.question_file import UploadedFile

IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif',
    '.tiff', '.avif', '.heic', '.heif', '.heics', '.heifs', '.hif',
})
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024


class UploadSizeExceeded(ValueError):
    """转换产物超限，与无效文件和基础设施故障分别反馈。"""


@dataclass(frozen=True)
class SourceUpload:
    filename: str
    content: bytes


async def _convert_uploads_with_client(
    uploads: Iterable[SourceUpload | UploadedFile],
    client: MCPClientManager,
) -> tuple[UploadedFile, ...]:
    prepared: list[UploadedFile] = []
    total = 0
    for upload in uploads:
        if isinstance(upload, SourceUpload):
            extension = PurePath(upload.filename).suffix.lower()
            if extension not in SUPPORTED_EXTENSIONS:
                raise ValueError('不支持的文件类型')
            result = await client.convert_file(
                upload.content,
                filename=upload.filename,
                operation='auto',
            )
            upload = UploadedFile(type='images', content=result.content)
        total += len(upload.content)
        if len(upload.content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise UploadSizeExceeded('转换后的附件每份最多 10 MiB、合计最多 20 MiB，请拆分文件后重新上传')
        prepared.append(upload)
    return tuple(prepared)


async def prepare_uploads_async(
    uploads: Iterable[SourceUpload | UploadedFile],
    *,
    mcp_client: MCPClientManager | None = None,
) -> tuple[UploadedFile, ...]:
    """异步转换探索上传附件；全部成功且通过体积上限校验后返回，不写文件或变更任务状态。"""
    upload_list = list(uploads)
    if not any(isinstance(upload, SourceUpload) for upload in upload_list):
        # 无待转换原始文件时仅做体积校验，避免无意义的 MCP 连接。
        prepared: list[UploadedFile] = []
        total = 0
        for upload in upload_list:
            total += len(upload.content)
            if len(upload.content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise UploadSizeExceeded('转换后的附件每份最多 10 MiB、合计最多 20 MiB，请拆分文件后重新上传')
            prepared.append(upload)
        return tuple(prepared)

    if mcp_client is not None:
        return await _convert_uploads_with_client(upload_list, mcp_client)

    async with MCPClientManager() as managed_client:
        return await _convert_uploads_with_client(upload_list, managed_client)


def prepare_uploads(
    uploads: Iterable[SourceUpload | UploadedFile],
    *,
    mcp_client: MCPClientManager | None = None,
) -> tuple[UploadedFile, ...]:
    """同步兼容入口（如在线程池中调用）；底层经由 MCP `file_conversion` 执行转换。"""
    return asyncio.run(prepare_uploads_async(uploads, mcp_client=mcp_client))
