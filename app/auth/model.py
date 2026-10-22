"""认证后统一使用的服务器端员工身份。"""

from typing import Annotated

from app.model.personnel import PlatformCode

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    userid: str = Field(min_length=1, max_length=128, strict=True, description="可信的员工 userid；开发模式来自服务端配置。")
    name: str | None = Field(default=None, max_length=200, description="员工姓名；历史资料未知时为空。")
    department_id: int | None = Field(default=None, ge=0, description="员工所属的顶级部门 ID；开发模拟部门默认使用 0。")
    department_name: str | None = Field(default=None, max_length=200, description="顶级部门名称。")
    avatar_url: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}\.webp$',
        description="头像文件名，不包含目录或接口路径；读取接口将在后续阶段接入，未设置时为空。")
    platform_permissions: dict[PlatformCode, Annotated[int, Field(strict=True, ge=0)]] = Field(
        default_factory=dict, description="以平台代码为键、文件访问权限等级为值的映射，不代表平台管理权限。"
    )
    managed_platforms: list[Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')]] = Field(
        default_factory=list, description="用户具有管理权限的平台代码列表，来自管理员关联；与文件访问等级独立。")

    @field_validator('userid', 'name', 'department_name')
    @classmethod
    def reject_blank(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError('用户标识和名称不能为空白或包含首尾空白')
        return value

    @field_validator('managed_platforms')
    @classmethod
    def normalize_managed_platforms(cls, values: list[str]) -> list[str]:
        return sorted(set(values))
