"""文件检索模型参数；身份、等级、访问令牌只存在宿主回调。"""
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.model.personnel import PlatformCode


class RetrievalModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)


class SearchFilesArguments(RetrievalModel):
    """根据文件名称、内容线索或完整通行证查找注册中心文件。通行证按单个完整值精确匹配，可结合平台代码过滤。保留必要条件、否定和替代关系，不补造未知信息。可以在已有结果集内继续查找。返回结果集引用、数量及查询说明；需要了解具体文件时使用view_file_candidates。检索命中不代表原文已经核实。"""
    query: str = Field(strict=True,min_length=1,max_length=10000,description='完整自然语言查找需求，保留名称、内容、必要条件、否定及AND/OR关系；按通行证查找时提供原始完整通行证值，不猜测或改写。')
    platform_codes: list[PlatformCode] | None = Field(default=None,min_length=1,
        description='可选的准确平台代码列表；省略或null不额外限制平台，空列表或重复项不合法。与结果集范围取交集，始终受用户权限约束。')
    result_id: str | None = Field(default=None,strict=True,min_length=1,
        description='可选的search_files返回的完整最终结果集ID；传入后仅在其中继续查询。省略或null使用用户可访问范围；失效时报错，不回退全库。')

    @model_validator(mode='after')
    def unique_platforms(self):
        if self.platform_codes is not None and len(set(self.platform_codes))!=len(self.platform_codes):
            raise ValueError('平台代码不能重复')
        return self


class NameQuery(RetrievalModel):
    """按文件名称关键词查找；保留已知主体、标的、编号，不补造名称。可在已有集合内继续收窄，命中不证明内容条件成立。"""
    query: str = Field(strict=True,min_length=1,max_length=1000,
        description='简短的文件名称或名称关键词，例如“设备采购协议”；保留已知名称线索，不写成问题或摘要。')
    result_id: str | None = Field(default=None,strict=True,min_length=1,
        description='本次检索返回的完整候选结果集ID；传入限定集合，省略或null回到本次初始范围。不能传文件ID，失效不回退。')


class SummaryQuery(RetrievalModel):
    """按摘要内容查找，关键词与语义联合召回。根据已知线索拟写简短正式摘要，保留否定和条件，不补造主体、金额、日期或约定。摘要命中仍须原文核实。"""
    query: str = Field(strict=True,min_length=1,max_length=10000,
        description='根据已知内容拟写的简短正式摘要，例如“设备采购，约定验收后付款及质量保证”；只整理用户线索，不补充未知事实。')
    result_id: str | None = Field(default=None,strict=True,min_length=1,
        description='本次检索返回的完整候选结果集ID；传入后在该集合摘要内查询，省略或null回到初始范围。失效引用报错。')


class PassportQuery(RetrievalModel):
    """按单个完整通行证精确查找持有它的文件。一份文件可持有多个通行证，同一通行证也可对应多份文件。保留原值、大小写和空格，不分词、不拆分或猜测通行证；可在已有结果集内筛选，并始终遵守本次平台和权限范围。"""
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=False)

    passport: str = Field(strict=True,min_length=1,max_length=512,
        description='一个非空白的完整通行证字符串，最多512字符；精确匹配文件通行证数组中的一个元素。保留首尾空格和大小写，不传数组，不拆分逗号或看起来像JSON的字符串。')
    result_id: str | None = Field(default=None,strict=True,min_length=1,
        description='本次检索返回的完整候选结果集ID；传入后只在该集合内匹配通行证，省略或null使用本次初始范围。失效引用报错，不扩大范围。')

    @field_validator('passport')
    @classmethod
    def nonblank_passport(cls, value):
        if not value.strip():
            raise ValueError('通行证不能全为空白')
        return value


class UnionResults(RetrievalModel):
    """满足两组条件任意一组时，分别在相同初始范围查询，再合并两结果集的并集并去重。不要先串行收窄来表达OR，不为抬分反复合并同源结果。"""
    left_result_id: str = Field(strict=True,min_length=1,description='本次检索第一张非空候选集合的完整ID；不能传文件ID或外部结果引用。')
    right_result_id: str = Field(strict=True,min_length=1,description='本次检索第二张非空候选集合的完整ID；与左表相同时复用原表。一路为空直接选择另一路，无需合并。')


class FinishRetrieval(RetrievalModel):
    """选择本次最终候选集合并结束检索。不提供结果集表示未找到合适候选；说明未核实条件、未覆盖分支及召回限制，不虚构原文事实。"""
    result_id: str | None = Field(default=None,strict=True,min_length=1,description='最终提交的完整候选结果集ID；省略或null表示没有合适候选，伪造或失效引用不是空结果。')
    explanation: str = Field(strict=True,min_length=1,max_length=2000,description='简洁说明使用的条件与尚未核实的限制；失败不能解释为没有相关文件。')


class ViewFileCandidatesArguments(RetrievalModel):
    """分页查看文件检索的最终候选，每条包含文件ID（注册中心ID）、文件名称、来源平台代码、来源平台文件ID和通行证列表。来源平台文件ID是文件在该来源平台内的唯一标识，与来源平台代码一起用于向该平台定位文件，例如向来源平台询问该文件的关联资料或已有风险标注；不能替代本地原文阅读工具需要的注册中心文件ID。未登记时显示“资料未提供”，不得猜测。通行证是文件关联标识，不代表访问授权。省略页码时首次为第一页，随后下一页；指定页码可重看。候选不表示原文已核实。"""
    result_id: str = Field(strict=True,min_length=1,description='search_files返回的完整最终结果集ID，不接受文件ID、其他任务引用或自行编造的标识。')
    page: int | None = Field(default=None,strict=True,ge=1,description='从1开始的页码；省略或null首次查看第一页，之后下一页。末页不循环，显式页码可以重看。')
