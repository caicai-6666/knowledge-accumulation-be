"""Excel整表导出：一张工作表一页，页面随内容扩展，不压缩到A4。"""
from io import BytesIO
import json
from xml.etree import ElementTree
from zipfile import ZipFile, BadZipFile
import olefile
from .model import ConversionError, ConvertedPDF
from .office import MAX_BYTES, MAX_UNPACKED_BYTES, convert_office_to_pdf


def excel_format(content: bytes) -> str | None:
    if content.startswith(b'PK'):
        try:
            with ZipFile(BytesIO(content)) as archive:
                items=archive.infolist()
                if len(items)>10000 or sum(item.file_size for item in items)>MAX_UNPACKED_BYTES:
                    raise ConversionError('document_too_large','Excel解压后内容过大。')
                types=archive.getinfo('[Content_Types].xml')
                if types.file_size>1024*1024:
                    raise ConversionError('invalid_excel','Excel类型声明过大。')
                root=ElementTree.fromstring(archive.read(types))
                # OPC允许按扩展名声明默认类型；显式Override优先，不能误接收XLSM。
                defaults={item.get('Extension'):item.get('ContentType') for item in root if item.tag.endswith('}Default')}
                overrides={item.get('PartName'):item.get('ContentType') for item in root if item.tag.endswith('}Override')}
                if overrides.get('/xl/workbook.xml',defaults.get('xml'))=='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml':
                    archive.getinfo('xl/workbook.xml')
                    return 'XLSX'
        except (BadZipFile,KeyError,ElementTree.ParseError,RuntimeError,OSError):return None
    elif content.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        try:
            with olefile.OleFileIO(BytesIO(content)) as archive:
                if archive.exists('Workbook') or archive.exists('Book'):return 'XLS'
                if archive.exists('EncryptedPackage'):
                    raise ConversionError('encrypted_document','Excel已加密，请提供无需密码的文件。')
        except ConversionError:raise
        except (OSError,ValueError):return None
    return None


EXCEL_PDF_FILTER = 'pdf:calc_pdf_Export:' + json.dumps({
    # 整表模式忽略纸张/打印范围，包含隐藏工作表；不截取已保存打印区域。
    'SinglePageSheets':{'type':'boolean','value':'true'},
    'ReduceImageResolution':{'type':'boolean','value':'false'},
    'UseLosslessCompression':{'type':'boolean','value':'true'},
    'ExportBookmarks':{'type':'boolean','value':'true'},
},separators=(',',':'))


def convert_excel_to_pdf(content: bytes, *, executable=None, timeout_seconds=60,
                         temporary_directory=None) -> ConvertedPDF:
    """保留可缩放的文本/线条，不重写单元格、宽高、合并或公式。"""
    if not isinstance(content,bytes):raise TypeError('Excel转换只接受内存bytes')
    if not content:raise ConversionError('empty_file','Excel内容为空。')
    if len(content)>MAX_BYTES:raise ConversionError('input_too_large','Excel文件超过32MiB。')
    kind=excel_format(content)
    if kind is None:raise ConversionError('unsupported_format','文件不是支持的XLSX或XLS工作簿。')
    return convert_office_to_pdf(content,kind=kind,export_filter=EXCEL_PDF_FILTER,
        executable=executable,timeout_seconds=timeout_seconds,temporary_directory=temporary_directory)
