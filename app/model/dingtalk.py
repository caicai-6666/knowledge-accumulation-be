"""经钉钉验证的员工资料与部门信息，仅保留初始化所需字段。"""

from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


class DingTalkEmployee(BaseModel):
    userid: str = Field(min_length=1, max_length=128, strict=True)
    name: str = Field(min_length=1, max_length=200, strict=True)
    dept_id_list: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=1)
    avatar: str | None = Field(default=None, strict=True)
    manager_userid: str | None = Field(default=None, strict=True, max_length=128,
        description='用户详情中的直属主管企业 userid；未维护时为空。')
    title: str | None = Field(default=None, strict=True, max_length=200,
        description='用户详情中的职位名称；未维护时为空。')

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

    @model_validator(mode='before')
    @classmethod
    def normalize_root_parent(cls, value):
        # 钉钉根部门（1）可省略 parent_id；只有根部门能据此确定不存在父部门。
        if isinstance(value, dict) and value.get('dept_id') == 1 and 'parent_id' not in value:
            return {**value, 'parent_id': 0}
        return value

    @field_validator('name')
    @classmethod
    def reject_blank(cls, value):
        if not value.strip():
            raise ValueError('部门名称不能为空')
        return value
