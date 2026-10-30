"""画像查询、逐条增删契约及纯展示函数；执行由宿主按可信身份装配。"""

from dataclasses import dataclass
from pydantic import Field, TypeAdapter, model_validator
from app.model.assistant_portrait import PortraitModel, PortraitSubject, PortraitRecord, PortraitGroup, Identifier, PreferenceText

class SearchPortraitsArguments(PortraitModel):
    name: PreferenceText = Field(max_length=200, description='所属用户姓名或其他用户助理名称的检索关键词，例如“张明”或“李娜的助理”；按姓名全文相关性检索，字面包含补充短词；末尾“的助理”会移除后查询员工姓名。只查当前助理私有画像，同名时查看结果再确认。')


class SearchPortraitsResult(PortraitModel):
    query_id: Identifier | None = Field(description='成功建立的画像结果集 ID，交给 view_portrait_query 翻页；没有匹配画像时为空，不生成空结果集。')
    total_subjects: int = Field(strict=True, ge=0, description='有匹配画像的目标对象数量；同一目标的多条偏好只计一个对象。')
    total_portraits: int = Field(strict=True, ge=0, description='全部匹配对象所包含的画像条数，可以大于对象数量。')
    total_pages: int = Field(strict=True, ge=0, description='按对象分组后的总页数；无匹配时为 0。')

    @model_validator(mode='after')
    def validate_counts(self):
        if self.total_subjects == 0:
            if self.query_id is not None or self.total_portraits != 0 or self.total_pages != 0:
                raise ValueError('空查询不能生成结果集或非零计数')
        elif (self.query_id is None or self.total_portraits < self.total_subjects
              or not 1 <= self.total_pages <= self.total_subjects):
            raise ValueError('画像查询结果与对象、画像或分页计数不一致')
        return self


class ViewPortraitQueryArguments(PortraitModel):
    query_id: Identifier = Field(description='search_portraits 返回的完整 query_id，必须原样使用；不能使用人员目录或其他工具的查询 ID。')
    page: int = Field(strict=True, ge=1, description='需要查看的页码，从 1 开始且不超过 total_pages；同一页可重复查看，结果集失效时重新查询。')


class ViewPortraitQueryResult(PortraitModel):
    query_id: Identifier = Field(description='本页所属的画像结果集 ID。')
    page: int = Field(strict=True, ge=1, description='实际返回的页码，从 1 开始。')
    total_pages: int = Field(strict=True, ge=1, description='该结果集按对象分页的总页数。')
    has_next: bool = Field(strict=True, description='当前页后还有页面时为 true。')
    total_subjects: int = Field(strict=True, ge=1, description='当前结果集的目标对象总数；分层正文通过可折叠资料页单独展示，不写入此元数据。')

    @model_validator(mode='after')
    def validate_page(self):
        if self.page > self.total_pages or self.has_next != (self.page < self.total_pages):
            raise ValueError('页码和后续页状态不一致')
        return self


class AddPortraitArguments(PortraitModel):
    subject: PortraitSubject = Field(description='新增这一条画像的目标；从个人资料、画像结果或已确认的人员查询结果取得准确 userid。')
    preference: PreferenceText = Field(description='本次要新增的一条偏好，写明必要情境与依据；一次只新增一条，UTF-8 内容不超过65535字节，不替换其他画像。')


class AddPortraitResult(PortraitModel):
    subject: PortraitSubject = Field(description='存储端确认新增画像所关联的目标 userid。')
    portrait: PortraitRecord = Field(description='成功新增的单条偏好及其稳定数字 key；不代表覆盖或更新了该目标的全部画像。')


class DeletePortraitArguments(PortraitModel):
    key: int = Field(strict=True, ge=1, le=2**64-1, description='从 view_portrait_query 或 add_portrait 成功结果取得的单条偏好 key，必须是原样的正整数；只删除这一条，不填用户ID、页内序号、姓名或结果集ID。')


class DeletePortraitResult(PortraitModel):
    key: int = Field(strict=True, ge=1, le=2**64-1, description='存储端已核对的单条偏好稳定数字 key。')
    deleted: bool = Field(strict=True, description='存储确认删除该条时为 true；在当前助理私有范围内查询确认已不存在时为 false，不泄露其他助理的记录。')


def render_portrait_groups(groups, *, owner_userid: str, query_id: str | None = None, page: int, total_pages: int) -> str:
    """结果页→目标用户→偏好 key 和正文；只渲染已有标识，不现场生成删除序号。"""
    owner = PortraitSubject(userid=owner_userid)
    if query_id is not None:
        ViewPortraitQueryArguments(query_id=query_id, page=page)
    if type(page) is not int or type(total_pages) is not int or not 1 <= page <= total_pages:
        raise ValueError('总页数或当前页码无效')
    rows = TypeAdapter(tuple[PortraitGroup, ...]).validate_python(groups)
    if not rows:
        raise ValueError('空结果不能渲染为有效分页')
    if len({group.subject.userid for group in rows}) != len(rows):
        raise ValueError('同一目标不能拆为多个画像分组')
    keys = [row.key for group in rows for row in group.portraits]
    if len(set(keys)) != len(keys):
        raise ValueError('偏好 key 不能跨目标重复')

    def single_line(text):
        return text.replace('\r', '\\r').replace('\n', '\\n')

    lines = ['════════════ 用户画像查询结果 ════════════']
    if query_id is not None:
        lines.append('结果集ID：' + query_id)
    lines += [f'第 {page} / {total_pages} 页', '偏好 key 是固定删除标识，不是本页序号。']
    for group in rows:
        name = single_line(group.name)
        label = name + '（所属用户）' if group.subject == owner else name + '的助理'
        lines += ['', '◆ ' + label, '用户ID：' + group.subject.userid]
        # 只按存储端的稳定 key 排序；分页、删除和再次查询都不能现场重新编号。
        for item in sorted(group.portraits, key=lambda row: row.key):
            lines += ['', f'  偏好 key：{item.key}', '  偏好内容：']
            lines.extend('    ' + line for line in item.preference.split('\n'))
        lines += ['', '──────── 用户画像结束 ────────']
    lines += ['', f'可继续查看第 {page + 1} 页。' if page < total_pages else '已到最后一页。',
              f'════════════ 第 {page} / {total_pages} 页结束 ════════════']
    return '\n'.join(lines)


@dataclass(frozen=True)
class PortraitToolContract:
    name: str
    description: str
    arguments: type[PortraitModel]
    result: type[PortraitModel]


# 契约清单本身不注册工具；查询及增删模块由消费宿主按当前身份装配。
PORTRAIT_TOOL_CONTRACTS = (
    PortraitToolContract('search_portraits',
        '当需要查找所属用户或其他助理的画像时，按名称检索当前助理私有记录，返回结果集 ID 和计数；随后用 view_portrait_query 查看。',
        SearchPortraitsArguments, SearchPortraitsResult),
    PortraitToolContract('view_portrait_query',
        '当已取得画像结果集 ID 时，按页查看程序分层渲染的对象及其全部偏好；结果集失效时重新检索。',
        ViewPortraitQueryArguments, ViewPortraitQueryResult),
    PortraitToolContract('add_portrait',
        '当需要记录一条有依据的偏好时，向准确目标追加一条画像；返回新偏好的数字 key，不替换其他条目，结果不确定时不能声称已保存或盲目重试。',
        AddPortraitArguments, AddPortraitResult),
    PortraitToolContract('delete_portrait',
        '当需要删除一条过时或不再适用的画像时，使用查看结果中的数字 key，仅删除当前助理私有范围内这一条，不清空该对象的其他画像。',
        DeletePortraitArguments, DeletePortraitResult),
)
