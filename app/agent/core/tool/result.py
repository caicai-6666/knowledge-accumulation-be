"""工具执行事实与提交回调；内容类别独立于成功、失败和不确定状态。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator
from ..json_codec import render_json

ReferenceText = Annotated[str, StringConstraints(strict=True, pattern=r'\S')]


class ToolPageUnavailable(LookupError):
    """临时资料已释放；展示层保留来源并明确标记不可读，不伪造正文。"""


class ResultModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, revalidate_instances='always')


class OrdinaryToolContent(ResultModel):
    type: Literal['ordinary'] = Field(default='ordinary', description='普通结果，只展示 tool_result，不注入临时页面。')


class ToolPageReference(ResultModel):
    resource_id: ReferenceText = Field(description='由工具管理的资源标识，不携带正文或图片 Base64；不直接注入模型。')
    display_id: ReferenceText = Field(description='本次页面展示的独立标识；重新打开同一页必须生成新标识，仅供程序使用。')
    locator: ReferenceText = Field(description='资源内部的业务位置，例如 page:3 或 lines:51-100，不包含服务器路径或内部节点标识。')
    media_type: Literal['text', 'table', 'image'] = Field(description='实际页面展示形式：文本、表格或图像；正文由解析器按需取得。')
    description: ReferenceText = Field(description='模型可见的简短业务来源说明，不包含整页正文、缓存信息或内部工程标识。')


class FoldableToolContent(ResultModel):
    type: Literal['foldable'] = Field(default='foldable', description='可折叠结果，只保存页面引用，大容量正文不进入工具回执。')
    pages: list[ToolPageReference] = Field(min_length=1, description='本次打开的页面引用，每项对应一次独立展示。')

    @model_validator(mode='after')
    def unique_displays(self):
        ids = [page.display_id for page in self.pages]
        if len(ids) != len(set(ids)):
            raise ValueError('同次返回的页面展示标识不能重复')
        return self


ToolContent = Annotated[OrdinaryToolContent | FoldableToolContent, Field(discriminator='type')]
ToolContentType = Literal['ordinary', 'foldable']


class ToolGuidance(ResultModel):
    source: Literal['tool', 'workspace'] = Field(description='可信程序提示的来源；不使用资料正文推断来源。')
    message: dict[str, Annotated[str, Field(description='提示消息的角色或正文，只允许 role=user 与非空 content。')]] = Field(
        description='统一提示构造器生成的 user 消息，不接收其他角色或额外字段。')

    @model_validator(mode='after')
    def validate_message(self):
        if (set(self.message) != {'role', 'content'} or self.message['role'] != 'user'
                or not self.message['content'].strip()):
            raise ValueError('工具提示必须是非空 user 消息')
        return self


class ToolExecutionResult(ResultModel):
    status: Literal['succeeded', 'failed', 'unknown'] = Field(description='执行事实：成功、明确未成功或结果不确定；不确定时停止且禁止自动重放。')
    content: ToolContent = Field(default_factory=OrdinaryToolContent, description='程序消费的普通或可折叠内容，与执行状态独立。')
    tool_result: dict[str, JsonValue] = Field(description='模型可见的轻量结果，不包含私有审计、页面正文或图片 Base64。')
    system_guidence: list[ToolGuidance] = Field(default_factory=list, description='可信工具产生的全部提示，按顺序保留，不用后一个覆盖前一个。')

    @model_validator(mode='after')
    def validate_result(self):
        if self.status != 'succeeded' and self.content.type != 'ordinary':
            raise ValueError('失败或不确定结果不能发布页面')
        render_json(self.tool_result)  # JSON 值仍可能容纳非有限浮点数，序列化边界继续拒绝。
        return self


@dataclass(frozen=True)
class ToolExecution:
    """宿主内部信封；回调不属于可序列化结果，也不进入模型上下文。"""
    result: ToolExecutionResult
    after_commit: Callable[[], None | Awaitable[None]] | None = None
    # 输出工具的权威正文；执行后钩子可替换，宿主在提交前同步到已接受参数。
    output: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'result', ToolExecutionResult.model_validate(self.result))
        if self.after_commit is not None and (not callable(self.after_commit) or self.result.status != 'succeeded'):
            raise ValueError('只有成功执行才能附带提交回调')
        if self.output is not None and (not isinstance(self.output, str) or not self.output.strip()
                                        or self.result.status != 'succeeded'):
            raise ValueError('只有成功执行才能返回非空输出正文')
