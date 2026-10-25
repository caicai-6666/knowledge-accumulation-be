"""模式注入的临时请求提示协议；实现及每次运行的状态由模式工厂提供。"""
from typing import Protocol


class RequestGuidance(Protocol):
    def before_request(self, round_number: int) -> dict[str, str] | None:
        """返回本次模型请求专用消息；不写入历史、工作区或摘要。"""
        ...

    def after_output(self, kind: str) -> None:
        """只在公开输出提交并成功交付后通知；失败和取消不调用。"""
        ...
