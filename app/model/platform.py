"""员工初始化与平台查询共用的中间件可见平台目录契约。"""

from pydantic import BaseModel, ConfigDict, Field

from app.model.personnel import PlatformCode


class PlatformInfo(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra='forbid')

    code: PlatformCode = Field(description='平台代码。')
    name: str = Field(min_length=1, pattern=r'\S', description='平台名称。')
    description: str = Field(min_length=1, pattern=r'\S', description='平台用途说明。')
    is_alive: bool = Field(description='中间件平台会话是否存活，不代表业务健康；离线平台仍在列表中。')
