"""与现有人员 DDL 对齐的数据契约；权限等级与管理员身份互相独立。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

PlatformCode = Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')]


class EmployeeName(BaseModel):
    """人员目录的最小投影；不给通信目标查询附带权限或其他身份资料。"""
    model_config = ConfigDict(frozen=True, extra='forbid')

    userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'\S', description='用户 userid，也是其唯一助理的通信标识，保留原始大小写。')
    username: str = Field(strict=True, min_length=1, max_length=200, pattern=r'\S', description='权威员工表中保存的用户名。')


class Department(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    id: int = Field(strict=True, ge=0, description='公司顶级部门 ID，真实部门沿用钉钉 ID，开发模拟可使用 0。')
    name: str = Field(strict=True, min_length=1, max_length=200, pattern=r'\S', description='公司顶级部门名称。')


class PlatformAdmin(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    platform_code: PlatformCode = Field(description='具有管理权限的平台代码，不代表文件权限等级。')
    employee_userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'\S', description='关联员工 userid。')
