"""经钉钉验证的员工资料与部门信息，仅保留初始化所需字段。"""

from typing import Annotated

from pydantic import BaseModel, Field, field_validator


class DingTalkEmployee(BaseModel):
    userid: str = Field(min_length=1, max_length=128, strict=True)
    name: str = Field(min_length=1, max_length=200, strict=True)
    dept_id_list: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=1)
    avatar: str | None = Field(default=None, strict=True)

    @field_validator('userid', 'name')
    @classmethod
    def reject_blank(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError('员工标识与姓名不能为空或含首尾空白')
        return value


class DingTalkDepartment(BaseModel):
    dept_id: int = Field(strict=True, gt=0)
    parent_id: int = Field(strict=True, ge=0)
    name: str = Field(min_length=1, max_length=200, strict=True)

    @field_validator('name')
    @classmethod
    def reject_blank(cls, value):
        if not value.strip():
            raise ValueError('部门名称不能为空')
        return value
