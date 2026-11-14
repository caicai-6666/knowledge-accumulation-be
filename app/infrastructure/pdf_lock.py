"""MuPDF 非线程安全；附件转换和 Core 阅读共用进程内可重入锁。"""
from functools import wraps
from threading import RLock

_PDF_LOCK = RLock()


def serialized_pdf_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _PDF_LOCK:
            return function(*args, **kwargs)
    return wrapped
