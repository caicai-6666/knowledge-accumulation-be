"""机器人附件仅接受 PDF 和栅格图片；全部转换在内存中完成，不使用原文件名定位文件。"""
from io import BytesIO
from pathlib import PurePath

from PIL import Image, ImageOps
import pymupdf


IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif'}
IMAGE_FORMATS = {'PNG', 'JPEG', 'WEBP', 'GIF', 'BMP', 'TIFF', 'AVIF'}


class AttachmentRejected(ValueError):
    """可明确向用户反馈的附件内容问题，不包含底层异常或下载链接。"""


def supported(part):
    return (part.type == 'picture' or part.type == 'file'
            and PurePath(part.filename).suffix.lower() in IMAGE_EXTENSIONS | {'.pdf'})


def validate_pdf(content):
    try:
        if not content.startswith(b'%PDF-'):
            raise ValueError()
        with pymupdf.open(stream=content, filetype='pdf') as document:
            if not document.is_pdf or document.needs_pass or document.page_count < 1:
                raise ValueError()
            # 打开每一页，避免只验证文件头就把明显损坏的附件交给助理。
            for page in document:
                if page.rect.is_empty or page.rect.is_infinite:
                    raise ValueError()
    except Exception:
        raise AttachmentRejected('PDF 损坏、加密或没有有效页面') from None
    return content


def to_pdf(part, content, settings, *, max_bytes):
    if not supported(part):
        raise AttachmentRejected('暂不支持该文件类型')
    if part.type == 'file' and PurePath(part.filename).suffix.lower() == '.pdf':
        result = validate_pdf(content)
    else:
        try:
            with Image.open(BytesIO(content)) as source, pymupdf.open() as document:
                if source.format not in IMAGE_FORMATS:
                    raise ValueError()
                frames = getattr(source, 'n_frames', 1)
                if frames > settings.max_image_frames:
                    raise AttachmentRejected('图片帧数超出限制')
                for index in range(frames):
                    source.seek(index)
                    if source.width * source.height > settings.max_image_pixels:
                        raise AttachmentRejected('图片尺寸超出限制')
                    # 保留多帧、修正拍摄方向，透明背景铺白；不静默丢弃后续帧。
                    with ImageOps.exif_transpose(source).convert('RGBA') as frame:
                        with Image.new('RGB', frame.size, 'white') as flattened:
                            flattened.paste(frame, mask=frame.getchannel('A'))
                            output = BytesIO()
                            flattened.save(output, format='PNG')
                            page = document.new_page(width=frame.width, height=frame.height)
                            page.insert_image(page.rect, stream=output.getvalue())
                # 重试必须得到相同内容；不生成时间戳或随机 PDF 文档 ID。
                result = document.tobytes(garbage=4, deflate=True, no_new_id=True)
        except AttachmentRejected:
            raise
        except Exception:
            raise AttachmentRejected('图片损坏或无法读取') from None
    if not result or len(result) > max_bytes:
        raise AttachmentRejected('文件大小超出限制')
    return result
