"""平台管理员展示资料，仅含姓名及头像文件名。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.model.personnel import PlatformCode


class PlatformAdminProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str = Field(strict=True, min_length=1, description='管理员姓名，历史未知时使用 userid。')
    avatar_url: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}\.webp$', description='头像文件名，无头像为 null。')


class PlatformAdminTable(BaseModel):
    """仅携带平台代码和 userid 的全量关联，归一化后按关系数核对。"""
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    admins: dict[PlatformCode, list[Annotated[str, Field(min_length=1, max_length=128)]]] = Field(
        description='完整平台管理员映射；省略平台将清空，不携带权限等级、展示资料或 token。')

    @field_validator('admins')
    @classmethod
    def normalize(cls, admins):
        if any(userid != userid.strip() or not userid.strip() for ids in admins.values() for userid in ids):
            raise ValueError('管理员 userid 不能含首尾空白或仅为空白')
        return {code: sorted(set(ids)) for code, ids in sorted(admins.items())}

    @property
    def relation_count(self) -> int:
        return sum(len(ids) for ids in self.admins.values())


class PlatformAdminTableResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    admin_count: int = Field(ge=0, description='中央确认的去重后平台管理员关系总数。')
