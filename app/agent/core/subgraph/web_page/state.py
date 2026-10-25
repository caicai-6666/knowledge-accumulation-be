"""HTML及处理候选只在子图内部流转；确认直出或精炼后才发布正文。"""
from typing import Literal
from typing_extensions import TypedDict
from .schema import WebPageRequest, WebPageResult
from .image import WebImage


class WebPageInput(TypedDict):
    request: WebPageRequest | dict


class WebPageOutput(TypedDict):
    result: WebPageResult


class WebPageState(WebPageInput, total=False):
    html: bytes
    extracted_text: str
    access_hint: str
    image_candidates: list[dict]
    images: tuple[WebImage, ...]
    selected_images: tuple[WebImage, ...]
    image_hint: str
    processing_mode: Literal["original", "refine"]
    content_hint: str | None
    has_content: bool
    refined_content: str
    hint: str | None
    final_url: str | None
    fetched_at: str | None
    error_code: str | None
    failed_stage: str | None
    result: WebPageResult
