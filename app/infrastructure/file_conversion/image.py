"""复用项目管理平台的无损图片转换，保留本项目多帧及 AVIF 能力。"""
from io import BytesIO
import warnings
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
import pymupdf
from .model import ConversionError, ConvertedPDF
from .pdf import inspect_pdf_openable, serialized_pdf_operation

register_heif_opener(thumbnails=False)
IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif',
    '.tiff', '.avif', '.heic', '.heif', '.heics', '.heifs', '.hif'})
IMAGE_FORMATS = frozenset({'PNG', 'JPEG', 'WEBP', 'GIF', 'BMP', 'TIFF', 'AVIF', 'HEIF'})


@serialized_pdf_operation
def convert_image_to_pdf(content: bytes, *, max_bytes=52428800,
                         max_pixels=40000000, max_frames=100) -> ConvertedPDF:
    if not isinstance(content, bytes):
        raise TypeError('图片转换只接受内存 bytes')
    if not content or len(content) > max_bytes:
        raise ConversionError('input_too_large', '图片为空或超过大小限制')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source, pymupdf.open() as document:
                kind = source.format
                if kind not in IMAGE_FORMATS:
                    raise ConversionError('unsupported_format', '图片格式不支持')
                frames = getattr(source, 'n_frames', 1)
                if frames > max_frames:
                    raise ConversionError('too_many_frames', '图片帧数超出限制')
                total_pixels = 0
                for index in range(frames):
                    source.seek(index)
                    total_pixels += source.width * source.height
                    if source.width * source.height > max_pixels:
                        raise ConversionError('too_many_pixels', '图片尺寸超出限制')
                    if total_pixels > max_pixels * 4:
                        raise ConversionError('too_many_pixels', '多帧图片累计尺寸超出限制，请拆分图片')
                    if source.mode in ('I', 'F') or source.mode.startswith('I;16'):
                        raise ConversionError('unsupported_pixel_mode', '暂不支持高位深或浮点图片，请转换为常规8位图片')
                    source.load()
                    with ImageOps.exif_transpose(source).convert('RGBA') as frame:
                        with Image.new('RGBA', frame.size, 'white') as background:
                            background.alpha_composite(frame)
                            with background.convert('RGB') as rgb:
                                output = BytesIO()
                                rgb.info.clear()
                                rgb.save(output, format='PNG')
                                # 保留原像素；仅约束 PDF 页面尺寸，不把长图压缩成 A4。
                                scale = min(1, 14400 / max(rgb.size))
                                page = document.new_page(width=rgb.width * scale, height=rgb.height * scale)
                                page.insert_image(page.rect, stream=output.getvalue())
                result = document.tobytes(garbage=4, deflate=True, no_new_id=True)
        if len(result) > max_bytes:
            raise ConversionError('output_too_large', '转换后的 PDF 超过大小限制')
        return ConvertedPDF(result, kind, inspect_pdf_openable(result))
    except ConversionError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ConversionError('too_many_pixels', '图片尺寸超出限制') from None
    except (OSError, ValueError, SyntaxError, RuntimeError):
        raise ConversionError('invalid_image', '图片损坏或无法读取') from None
