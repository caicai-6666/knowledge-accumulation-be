"""转换期间的 PDF 校验与串行访问；不负责保存文件。"""
import pymupdf
from app.infrastructure.pdf_lock import serialized_pdf_operation
from .model import ConversionError, ConvertedPDF


@serialized_pdf_operation
def inspect_pdf_openable(content: bytes) -> int:
    try:
        if not isinstance(content, bytes) or not content.startswith(b'%PDF-'):
            raise ValueError()
        with pymupdf.open(stream=content, filetype='pdf') as document:
            if not document.is_pdf or document.needs_pass or not document.page_count:
                raise ValueError()
            for page in document:
                if page.rect.is_empty or page.rect.is_infinite:
                    raise ValueError()
            return document.page_count
    except (ValueError, RuntimeError):
        raise ConversionError('invalid_pdf', 'PDF 损坏、加密或没有有效页面') from None


def convert_pdf(content: bytes, *, max_bytes: int) -> ConvertedPDF:
    if len(content) > max_bytes:
        raise ConversionError('output_too_large', '文件大小超出限制')
    return ConvertedPDF(content, 'PDF', inspect_pdf_openable(content))
