"""首次读取网页的输入输出契约；来源和缓存由外层管理。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from .image import WebImage


class WebPageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    url: HttpUrl = Field(description='外层根据来源标识解析出的 HTTP 或 HTTPS 地址。')
    focus: str = Field(min_length=1, description='网页阅读需求，说明目标对象、局部或全文范围，以及明确的摘要或翻译要求；默认保留相关原文及原语言。')

    @model_validator(mode='after')
    def no_credentials(self):
        if self.url.username or self.url.password:
            raise ValueError('公开网页地址不能包含登录凭据')
        return self


class WebPageResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    status: Literal['success', 'no_content', 'error'] = Field(description='success 为有效正文，no_content 为正常读取后未取得可用正文，error 为执行故障。')
    url: str = Field(description='原始请求地址。')
    final_url: str | None = Field(default=None, description='HTTP 重定向后的实际地址，未取得时为空。')
    fetched_at: str | None = Field(default=None, description='抓取时间，带时区的 ISO 8601 字符串，未抓取时为空。')
    content: str | None = Field(default=None, description='仅 status=success 时提供提取原文或精炼正文；无正文及执行失败时为空。')
    images: tuple[WebImage, ...] = Field(default=(), exclude=True, repr=False,
        description='直接保留或精炼选中的真实图片，只在当前任务内存中保留，不进入回执或持久化。')
    hint: str | None = Field(default=None, description='no_content 时必填友好的无正文原因；也用于执行失败或正文不完整等提示。')
    error_code: str | None = Field(default=None, description='仅执行失败时提供机器可读原因；success/no_content 时为空。')
    failed_stage: Literal['fetch_html', 'extract_text', 'refine_content', 'preserve_original', 'finalize'] | None = Field(
        default=None, description='仅执行失败时提供阶段；success/no_content 时为空。')

    @model_validator(mode='after')
    def validate_outcome(self):
        if self.status == 'success':
            if not self.content or self.error_code is not None or self.failed_stage is not None:
                raise ValueError('成功结果必须包含正文且不得包含错误标记')
        elif self.status == 'no_content':
            if self.content is not None or self.images or not self.hint or self.error_code is not None or self.failed_stage is not None:
                raise ValueError('无正文是正常结果，必须说明原因，不携带正文、图片或错误标记')
        elif self.content is not None or self.images or not self.error_code or not self.hint or not self.failed_stage:
            raise ValueError('失败结果必须包含原因和阶段，且不能返回正文')
        return self


class WebPageRefinement(BaseModel):
    """模型普通 JSON 输出，Schema 只用于提示与本地校验。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    evidence: list[str] = Field(description='支持范围判断和内容选择的简短原文依据，不重复整篇正文；无相关依据时可为空列表。')
    reasoning: str = Field(min_length=1, description='简要说明目标对象、相关性、必要上下文及排除理由；区分噪声、不相关正文和归属不明，不补充外部事实。')
    type: Literal['content', 'no_content'] = Field(description='content 表示有可归属且与重点相关的有效信息；no_content 表示无可用信息，需要结束本次读取。')
    result: str = Field(min_length=1, description='content 时默认保留所需范围的原语言、原句、结构及条件；全文要求覆盖主体正文，明确要求摘要或翻译时才转换。缺失内容单独说明，不伪称完整；no_content 时为具体友好的无有效信息提示。')
    selected_images: list[Annotated[int, Field(strict=True, ge=1, description='本次实际提供的图片编号，从 1 开始。')]] = Field(default_factory=list,
        description='保留给主助手查看的图片编号，必须来自本次实际提供的图片；无相关图片或 no_content 时为空列表。')
