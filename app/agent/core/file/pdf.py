"""内存 PDF 模型与预算内 PNG；所有 MuPDF 操作跨线程共用一把锁。"""
from dataclasses import dataclass
from functools import wraps
from math import ceil,isfinite
from threading import RLock
import pymupdf

_PDF_LOCK=RLock()


def serialized_pdf_operation(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        with _PDF_LOCK:return function(*args,**kwargs)
    return wrapped


class PDFOpenError(ValueError):
    """只携带可公开的格式失败，不传播底层 PDF 解析异常。"""


@dataclass(frozen=True)
class PDFRenderConfig:
    max_scale:float=2.0
    patch_size:int=32
    max_visual_tokens:int=2048

    def __post_init__(self):
        if not isfinite(self.max_scale) or self.max_scale<=0:raise ValueError('渲染比例必须为有限正数')
        for value in (self.patch_size,self.max_visual_tokens):
            if type(value) is not int or value<1:raise ValueError('视觉预算参数必须为正整数')


@dataclass(frozen=True)
class PDFPage:
    number:int
    png:bytes
    width:int
    height:int
    visual_tokens:int


def _tokens(width,height,config):
    return ceil(width/config.patch_size)*ceil(height/config.patch_size)


class FileModel:
    """持有已打开 PDF 和不可变页面；不保存 Base64、用户身份或阅读位置。"""
    @serialized_pdf_operation
    def __init__(self,content:bytes,*,config:PDFRenderConfig|None=None):
        self._document=None
        self._cache_owner=None
        self._pages={}
        self.config=config or PDFRenderConfig()
        if not isinstance(self.config,PDFRenderConfig):raise TypeError('页面配置不合法')
        if not isinstance(content,bytes) or not content:raise PDFOpenError('附件内容为空或无效')
        try:
            document=pymupdf.open(stream=content)
            try:
                if not document.is_pdf:raise PDFOpenError('附件实际格式不符合多图文件约定')
                if document.needs_pass:raise PDFOpenError('附件需要打开密码')
                if document.page_count<1:raise PDFOpenError('附件不包含页面')
                self.page_count=document.page_count
            except BaseException:
                document.close()
                raise
            self._document=document
        except PDFOpenError:raise
        except (pymupdf.FileDataError,RuntimeError,ValueError):
            raise PDFOpenError('附件损坏或无法解析') from None

    @property
    @serialized_pdf_operation
    def closed(self):return self._document is None

    @property
    @serialized_pdf_operation
    def cached_pages(self):return tuple(sorted(self._pages))

    @serialized_pdf_operation
    def get_page(self,number):
        if self._document is None:raise RuntimeError('文件模型已关闭')
        if type(number) is not int or not 1<=number<=self.page_count:raise ValueError('页码超出范围')
        if number in self._pages:return self._pages[number]
        page=self._document[number-1]
        width,height=page.rect.width,page.rect.height
        if not all(isfinite(value) and value>0 for value in (width,height)):raise ValueError('页面尺寸不合法')
        config=self.config
        def fits(scale):return _tokens(ceil(width*scale),ceil(height*scale),config)<=config.max_visual_tokens
        scale=config.max_scale
        if not fits(scale):
            lower,upper=0.0,scale
            for _ in range(64):
                middle=(lower+upper)/2
                if fits(middle):lower=middle
                else:upper=middle
            scale=lower*(1-1e-6)
        if scale<=0:raise ValueError('页面无法在视觉预算内渲染')
        pixmap=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
        tokens=_tokens(pixmap.width,pixmap.height,config)
        if tokens>config.max_visual_tokens:raise ValueError('实际页面渲染超出视觉预算')
        result=PDFPage(number,pixmap.tobytes('png'),pixmap.width,pixmap.height,tokens)
        self._pages[number]=result
        return result

    @serialized_pdf_operation
    def close(self):
        if self._document is not None:
            self._document.close()
            self._document=None
        self._pages.clear()
