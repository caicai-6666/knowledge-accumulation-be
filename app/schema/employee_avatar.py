"""注册中心人工刷新员工时的头像补齐契约。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FillEmployeeAvatarRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'^\S(?:.*\S)?$',
        description='需要补齐头像的现有员工准确userid，不是调用管理员的身份。')
    avatar_url: str | None = Field(strict=True, max_length=4096,
        description='钉钉员工资料中的头像HTTPS地址，仅接受服务端配置的允许域名；null或空串表示没有头像。')


class FillEmployeeAvatarResponse(BaseModel):
    userid: str = Field(description='本次补齐的目标员工userid。')
    status: Literal['created', 'existing', 'skipped'] = Field(
        description='created已补齐；existing已有头像且未覆盖；skipped未提供头像且未写入。')
    avatar_url: str | None = Field(pattern=r'^[a-f0-9]{32}\.webp$',
        description='已存储的头像文件名，不含目录；skipped时为null。')
