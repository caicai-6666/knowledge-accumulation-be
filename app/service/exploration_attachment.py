"""探索上传适配：原始文件仅用于转换，交给任务服务的仍是已校验 PDF。"""
from dataclasses import dataclass
from pathlib import PurePath

from app.infrastructure.file_conversion import (
    convert_excel_to_pdf, convert_image_to_pdf, convert_pdf, convert_word_to_pdf, convert_powerpoint_to_pdf,
)
from app.infrastructure.file_conversion.image import IMAGE_EXTENSIONS
from app.model.question_file import UploadedFile

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024


class UploadSizeExceeded(ValueError):
    """转换产物超限，与无效文件和基础设施故障分别反馈。"""


@dataclass(frozen=True)
class SourceUpload:
    filename: str
    content: bytes


def prepare_uploads(uploads):
    """在工作线程中转换；全部成功才返回，不写文件或变更任务状态。"""
    prepared, total = [], 0
    for upload in uploads:
        if isinstance(upload, SourceUpload):
            extension = PurePath(upload.filename).suffix.lower()
            if extension == '.pdf':
                result = convert_pdf(upload.content, max_bytes=MAX_FILE_BYTES)
            elif extension in {'.doc', '.docx'}:
                result = convert_word_to_pdf(upload.content)
            elif extension in {'.xls', '.xlsx'}:
                result = convert_excel_to_pdf(upload.content)
            elif extension in {'.ppt', '.pptx'}:
                result = convert_powerpoint_to_pdf(upload.content)
            elif extension in IMAGE_EXTENSIONS:
                result = convert_image_to_pdf(upload.content, max_bytes=MAX_FILE_BYTES)
            else:
                raise ValueError('不支持的文件类型')
            upload = UploadedFile(type='images', content=result.content)
        total += len(upload.content)
        if len(upload.content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise UploadSizeExceeded('转换后的附件每份最多 10 MiB、合计最多 20 MiB，请拆分文件后重新上传')
        prepared.append(upload)
    return tuple(prepared)
