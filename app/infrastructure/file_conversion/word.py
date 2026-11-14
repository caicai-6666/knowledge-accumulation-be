"""Word内容识别；导出复用Office独立进程。"""
from io import BytesIO
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree
import olefile
from .model import ConversionError, ConvertedPDF
from .office import MAX_BYTES, MAX_UNPACKED_BYTES, _executable, convert_office_to_pdf


def word_format(content: bytes) -> str | None:
    """仅识别非宏DOCX或含WordDocument流的旧DOC，不相信文件名。"""
    if content.startswith(b'PK'):
        try:
            with ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_UNPACKED_BYTES:
                    raise ConversionError('document_too_large', 'Word解压后内容过大。')
                types = archive.getinfo('[Content_Types].xml')
                if types.file_size > 1024 * 1024:
                    raise ConversionError('invalid_word', '文档类型声明过大。')
                root = ElementTree.fromstring(archive.read(types))
                for item in root:
                    if item.get('PartName') == '/word/document.xml' and item.get('ContentType') == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml':
                        archive.getinfo('word/document.xml')
                        return 'DOCX'
        except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError, OSError):
            return None
    elif content.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        try:
            with olefile.OleFileIO(BytesIO(content)) as archive:
                if archive.exists('WordDocument') and (archive.exists('0Table') or archive.exists('1Table')):
                    return 'DOC'
                if archive.exists('EncryptedPackage'):
                    raise ConversionError('encrypted_document', '文档已加密，请提供无需密码的Word或PDF。')
        except ConversionError:
            raise
        except (OSError, ValueError):
            return None
    return None


def convert_word_to_pdf(content: bytes, *, executable=None, timeout_seconds=60,
                        temporary_directory=None) -> ConvertedPDF:
    if not isinstance(content, bytes):raise TypeError('Word转换只接受内存bytes')
    if not content:raise ConversionError('empty_file', 'Word内容为空。')
    if len(content)>MAX_BYTES:raise ConversionError('input_too_large', 'Word文件超过32MiB。')
    kind=word_format(content)
    if kind is None:raise ConversionError('unsupported_format', '文件不是支持的DOCX或DOC文档。')
    return convert_office_to_pdf(content,kind=kind,export_filter='pdf:writer_pdf_Export',
        executable=_executable(executable),timeout_seconds=timeout_seconds,
        temporary_directory=temporary_directory)
