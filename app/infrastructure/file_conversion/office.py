"""Word/Excel/PowerPoint共用LibreOffice进程、临时目录、字体、超时和PDF校验。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from xml.etree import ElementTree
import os
import shutil
import signal
import subprocess
import time
import pymupdf
from .pdf import inspect_pdf_openable
from .pdf import serialized_pdf_operation
from app.config.file_conversion import load_office_converter_executable
from .model import ConversionError, ConvertedPDF
from .fonts import FONT_DIRECTORY, require_fonts, normalize_odf_fonts

MAX_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
_CONVERSIONS = BoundedSemaphore(2)


def _executable(explicit):
    explicit = explicit or load_office_converter_executable()
    candidates = [shutil.which(explicit) or explicit] if explicit else [shutil.which('soffice'), shutil.which('libreoffice'),
        '/Applications/LibreOffice.app/Contents/MacOS/soffice']
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise ConversionError('converter_unavailable', 'Office转换服务不可用，请安装LibreOffice。')


@serialized_pdf_operation
def _validated_pdf(content):
    pages = inspect_pdf_openable(content)
    with pymupdf.open(stream=content, filetype='pdf') as pdf:
        for page in pdf:
            if page.rect.is_empty or page.rect.is_infinite:
                raise ConversionError('invalid_output', '转换后的PDF包含无效页面。')
        # 清除生成时刻及文档标识；字体子集等仍可能不同，重试靠持久摘要复用。
        pdf.set_metadata({})
        pdf.del_xml_metadata()
        pdf.xref_set_key(-1, 'ID', 'null')
        result = pdf.tobytes(garbage=4, deflate=True, no_new_id=True)
    if len(result) > MAX_BYTES:
        raise ConversionError('output_too_large', '转换后的PDF超过32MiB。')
    return result, pages


def convert_office_to_pdf(content: bytes, *, kind, export_filter, executable=None, timeout_seconds=60,
                        temporary_directory=None) -> ConvertedPDF:
    """同步内存接口；仅转换期间落临时文件，完成/失败都清理。"""
    if not isinstance(content, bytes):raise TypeError('Office转换只接受内存bytes')
    if not content:raise ConversionError('empty_file', 'Office内容为空。')
    if len(content) > MAX_BYTES:raise ConversionError('input_too_large', 'Office文件超过32MiB。')
    if timeout_seconds <= 0:raise ValueError('转换超时必须大于0')
    binary = _executable(executable)
    require_fonts()
    if not _CONVERSIONS.acquire(timeout=timeout_seconds):
        raise ConversionError('converter_busy', 'Office转换服务忙。')
    try:
        with TemporaryDirectory(prefix='knowledge-office-', dir=temporary_directory) as directory:
            root = Path(directory).resolve()
            source = root / ('input.' + kind.lower())
            source.write_bytes(content)
            profile = root / 'profile';profile.mkdir()
            # 每份文档独立配置，避免连接桌面Office进程；禁用宏和外部链接自动更新。
            (profile/'user').mkdir()
            (profile/'user/registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>''')
            environment = dict(os.environ)
            # 每次转换显式扫描项目字体资产；系统字体仅用于目标字体不包含的字符。
            font_config = ElementTree.Element('fontconfig')
            if Path('/etc/fonts/fonts.conf').is_file():
                ElementTree.SubElement(font_config,'include').text='/etc/fonts/fonts.conf'
            for fonts in (str(FONT_DIRECTORY),'/System/Library/Fonts', '/Library/Fonts', str(Path.home()/'Library/Fonts')):
                ElementTree.SubElement(font_config,'dir').text=fonts
            ElementTree.SubElement(font_config,'cachedir').text=str(root/'font-cache')
            font_path=root/'fonts.conf'
            ElementTree.ElementTree(font_config).write(font_path,encoding='utf-8',xml_declaration=True)
            environment['FONTCONFIG_FILE']=str(font_path)
            deadline=time.monotonic()+timeout_seconds
            def export(input_path,filter_name,outdir):
                remaining=deadline-time.monotonic()
                if remaining<=0:raise ConversionError('conversion_timeout','Office转换超时。')
                command=[binary,'-env:UserInstallation='+profile.as_uri(),'--headless','--nologo',
                    '--nodefault','--nofirststartwizard','--convert-to',filter_name,'--outdir',str(outdir),str(input_path)]
                process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    start_new_session=True,env=environment)
                try:return process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait()
                    raise ConversionError('conversion_timeout','Office转换超时。') from None
            # 旧DOC/XLS/PPT和OOXML统一借助LibreOffice转临时ODF，再改字体；原件不回写。
            extension = {'XLS': 'ods', 'XLSX': 'ods', 'PPT': 'odp', 'PPTX': 'odp'}.get(kind, 'odt')
            intermediate=root/'normalized';intermediate.mkdir()
            normalized=intermediate/('input.'+extension)
            if export(source,extension,intermediate)!=0 or not normalized.is_file():
                raise ConversionError('conversion_failed','Office无法生成临时文档。')
            if normalized.stat().st_size>MAX_BYTES:
                raise ConversionError('document_too_large','临时Office文档超过32MiB。')
            normalized.write_bytes(normalize_odf_fonts(normalized.read_bytes()))
            code=export(normalized,export_filter,root)
            output = root/'input.pdf'
            if code != 0 or not output.is_file():
                raise ConversionError('conversion_failed', 'Office无法转换，可能损坏、加密或格式不支持。')
            if not 0 < output.stat().st_size <= MAX_BYTES:
                raise ConversionError('output_too_large', 'Office转换输出为空或超过32MiB。')
            result,pages = _validated_pdf(output.read_bytes())
            return ConvertedPDF(result, kind, pages)
    except ConversionError:raise
    except (OSError, ValueError, RuntimeError):
        raise ConversionError('conversion_failed', 'Office转换或输出校验失败。') from None
    finally:
        _CONVERSIONS.release()
