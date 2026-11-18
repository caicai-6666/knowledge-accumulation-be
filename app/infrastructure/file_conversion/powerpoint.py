"""PowerPoint内容识别与逐张幻灯片PDF导出；不依赖上传扩展名判断实际格式。"""
from io import BytesIO
import json
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import olefile

from .model import ConversionError, ConvertedPDF
from .office import MAX_BYTES, MAX_UNPACKED_BYTES, convert_office_to_pdf


PRESENTATION_TYPE = 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml'


def powerpoint_format(content: bytes) -> str | None:
    if content.startswith(b'PK'):
        try:
            with ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_UNPACKED_BYTES:
                    raise ConversionError('document_too_large', 'PowerPoint解压后内容过大。')
                if any(item.flag_bits & 1 for item in entries):
                    raise ConversionError('encrypted_document', 'PowerPoint已加密，请提供无需密码的文件。')
                # 宏演示不作为普通PPTX接收；不能仅改扩展名绕过限制。
                if any(item.filename.lower().endswith('vbaproject.bin') for item in entries):
                    return None
                names = [item.filename for item in entries]
                if len(names) != len(set(names)):
                    return None
                types = archive.getinfo('[Content_Types].xml')
                if types.file_size > 1024 * 1024:
                    raise ConversionError('invalid_powerpoint', 'PowerPoint类型声明过大。')
                root = ElementTree.fromstring(archive.read(types))
                defaults = {item.get('Extension'): item.get('ContentType') for item in root if item.tag.endswith('}Default')}
                overrides = {item.get('PartName'): item.get('ContentType') for item in root if item.tag.endswith('}Override')}
                if overrides.get('/ppt/presentation.xml', defaults.get('xml')) == PRESENTATION_TYPE:
                    archive.getinfo('ppt/presentation.xml')
                    return 'PPTX'
        except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError, OSError):
            return None
    elif content.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        try:
            with olefile.OleFileIO(BytesIO(content)) as archive:
                if archive.exists('EncryptedPackage') or archive.exists('EncryptionSummary'):
                    raise ConversionError('encrypted_document', 'PowerPoint已加密，请提供无需密码的文件。')
                if archive.exists('PowerPoint Document') and archive.exists('Current User'):
                    return 'PPT'
        except ConversionError:
            raise
        except (OSError, ValueError):
            return None
    return None


POWERPOINT_PDF_FILTER = 'pdf:impress_pdf_Export:' + json.dumps({
    # 包含隐藏幻灯片以免遗漏上传资料；不导出备注页，一张幻灯片对应一页。
    'ExportHiddenSlides': {'type': 'boolean', 'value': 'true'},
    'ExportNotesPages': {'type': 'boolean', 'value': 'false'},
    'ReduceImageResolution': {'type': 'boolean', 'value': 'false'},
    'UseLosslessCompression': {'type': 'boolean', 'value': 'true'},
}, separators=(',', ':'))


def convert_powerpoint_to_pdf(content: bytes, *, executable=None, timeout_seconds=60,
                              temporary_directory=None) -> ConvertedPDF:
    if not isinstance(content, bytes):
        raise TypeError('PowerPoint转换只接受内存bytes')
    if not content:
        raise ConversionError('empty_file', 'PowerPoint内容为空。')
    if len(content) > MAX_BYTES:
        raise ConversionError('input_too_large', 'PowerPoint文件超过32MiB。')
    kind = powerpoint_format(content)
    if kind is None:
        raise ConversionError('unsupported_format', '文件不是支持的PPTX或PPT演示文稿，宏演示不支持。')
    return convert_office_to_pdf(content, kind=kind, export_filter=POWERPOINT_PDF_FILTER,
        executable=executable, timeout_seconds=timeout_seconds, temporary_directory=temporary_directory)
