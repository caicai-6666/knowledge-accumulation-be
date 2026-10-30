"""助理私有画像的目标、单条偏好与查询分组；所有者由可信宿主绑定。"""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'^\S+$')]
PreferenceText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class PortraitModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class PortraitSubject(PortraitModel):
    userid: Identifier = Field(description='画像目标的准确员工 userid；等于当前助理所属 userid 时表示所属用户本人，否则表示该员工的 AI 助理，不另建员工画像。')


class PortraitRecord(PortraitModel):
    key: int = Field(strict=True, ge=1, le=2**64-1, description='存储端递增分配的单条偏好 key，稳定正整数；删除时原样填写，不是页内序号，删除后不重排、不复用，允许空号。')
    preference: PreferenceText = Field(description='一条完整偏好，可在文本中说明依据、适用情境及待确认之处；不代表权限或企业事实。')


class PortraitGroup(PortraitModel):
    subject: PortraitSubject = Field(description='该组画像的唯一目标；按 userid 聚合，不拆成员工和助理两组。')
    name: str = Field(strict=True, min_length=1, pattern=r'\S', description='目标员工的已确认姓名；展示其他对象时由程序附加“的助理”，不由模型推断姓名。')
    portraits: tuple[PortraitRecord, ...] = Field(min_length=1, description='该对象在本次查询快照中的全部单条画像；按稳定顺序展示，不因同一目标有多条而重复分页对象。')

    @model_validator(mode='after')
    def unique_records(self):
        if len({row.key for row in self.portraits}) != len(self.portraits):
            raise ValueError('同一对象的偏好 key 不能重复')
        return self
