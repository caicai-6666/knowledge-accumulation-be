"""转换器只返回已验证的 PDF，不负责文件下载、持久化或任务状态。"""
from dataclasses import dataclass


class ConversionError(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


@dataclass(frozen=True, slots=True)
class ConvertedPDF:
    content: bytes
    source_format: str
    page_count: int
