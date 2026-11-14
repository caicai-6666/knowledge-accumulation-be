"""钉钉附件格式路由；只将转换成功的 PDF 交给既有暂存和队列流程。"""
from pathlib import PurePath
from .file_conversion import convert_image_to_pdf, convert_word_to_pdf, convert_excel_to_pdf, convert_pdf
from .file_conversion.image import IMAGE_EXTENSIONS, IMAGE_FORMATS
from .file_conversion.model import ConversionError
from .file_conversion.pdf import inspect_pdf_openable


class AttachmentRejected(ValueError):
    """文件内容问题，可向用户说明，不回显底层异常。"""


class AttachmentConversionUnavailable(RuntimeError):
    """转换依赖暂不可用；保留入站记录重试，不拒收为坏文件。"""


def supported(part):
    return (part.type == 'picture' or part.type == 'file'
            and PurePath(part.filename).suffix.lower() in IMAGE_EXTENSIONS | {'.pdf', '.doc', '.docx', '.xls', '.xlsx'})


def validate_pdf(content):
    try:
        inspect_pdf_openable(content)
    except ConversionError as error:
        raise AttachmentRejected(error.reason) from None
    return content


def to_pdf(part, content, settings, *, max_bytes):
    if not supported(part):
        raise AttachmentRejected('暂不支持该文件类型，请提供图片、PDF、DOC/DOCX 或 XLS/XLSX')
    extension = PurePath(part.filename).suffix.lower() if part.type == 'file' else ''
    try:
        if extension == '.pdf':
            result = convert_pdf(content, max_bytes=max_bytes)
        elif extension in {'.doc', '.docx'}:
            result = convert_word_to_pdf(content)
        elif extension in {'.xls', '.xlsx'}:
            result = convert_excel_to_pdf(content)
        else:
            result = convert_image_to_pdf(content, max_bytes=max_bytes,
                max_pixels=settings.max_image_pixels, max_frames=settings.max_image_frames)
        if not result.content or len(result.content) > max_bytes:
            raise AttachmentRejected('转换后的 PDF 大小超出限制')
        return result.content
    except ConversionError as error:
        if error.code in {'converter_unavailable', 'font_unavailable', 'converter_busy'}:
            raise AttachmentConversionUnavailable(error.reason) from None
        raise AttachmentRejected(error.reason) from None
