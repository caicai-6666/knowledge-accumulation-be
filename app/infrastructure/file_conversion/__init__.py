"""格式独立的内存转换器；返回验证后的 PDF，不处理下载或持久化。"""
from .image import convert_image_to_pdf
from .word import convert_word_to_pdf
from .excel import convert_excel_to_pdf
from .powerpoint import convert_powerpoint_to_pdf
from .pdf import convert_pdf
from .model import ConversionError, ConvertedPDF

__all__ = ['convert_image_to_pdf', 'convert_word_to_pdf', 'convert_excel_to_pdf',
           'convert_pdf', 'convert_powerpoint_to_pdf', 'ConversionError', 'ConvertedPDF']
