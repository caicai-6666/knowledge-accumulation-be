"""与现有人员 DDL 对齐的数据契约；权限等级与管理员身份互相独立。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PlatformCode = Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')]
EmploymentStatus = Literal['employed', 'resigned']


class EmployeeWorkProfile(BaseModel):
    """员工任职资料；未知值明确为空，主管和部门负责人互相独立。"""

    model_config = ConfigDict(frozen=True, extra='forbid')

    manager_userid: str | None = Field(default=None, strict=True, max_length=128,
        description='钉钉直属主管的企业 userid；未维护时为空，不以部门主管替代。')
    title: str | None = Field(default=None, strict=True, max_length=200,
        description='钉钉维护的员工职位名称；未维护时为空。')

    @field_validator('manager_userid', 'title')
    @classmethod
    def normalize_optional_text(cls, value):
        if value is None:
            return None
        return value.strip() or None


class Department(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    id: int = Field(strict=True, ge=0, description='公司顶级部门 ID，真实部门沿用钉钉 ID，开发模拟可使用 0。')
    name: str = Field(strict=True, min_length=1, max_length=200, pattern=r'\S', description='公司顶级部门名称。')


class EmployeeName(BaseModel):
    """人员目录投影；完整部门帮助辨认同名对象，不附带权限或任职详情。"""
    model_config = ConfigDict(frozen=True, extra='forbid')

    userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'\S', description='用户 userid，也是其唯一助理的通信标识，保留原始大小写。')
    username: str = Field(strict=True, min_length=1, max_length=200, pattern=r'\S', description='权威员工姓名；姓名未知时使用其 userid 展示，不推测姓名。')
    employment_status: EmploymentStatus = Field(description='权威员工在职状态：employed 表示在职，resigned 表示离职；不表示助理在线或消息可送达。')
    departments: list[Department] = Field(default_factory=list,
        description='用户所属的全部一级部门，空列表表示未知；各部门并列归属，不代表主次或权限。')

    @field_validator('departments')
    @classmethod
    def order_departments(cls, value):
        # SQL 聚合不保证数组顺序；按身份稳定展示，不能按名称合并不同部门。
        return sorted(value, key=lambda item: item.id)


class PlatformAdmin(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    platform_code: PlatformCode = Field(description='具有管理权限的平台代码，不代表文件权限等级。')
    employee_userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'\S', description='关联员工 userid。')
