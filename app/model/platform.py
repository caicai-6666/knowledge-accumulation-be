"""平台公开基础字段与中间件目录读取契约；上游新增字段不自动扩散到门户响应。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.model.personnel import PlatformCode


class PlatformInfo(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra='forbid')

    code: PlatformCode = Field(description='平台代码。')
    name: str = Field(min_length=1, pattern=r'\S', description='平台名称。')
    description: str = Field(min_length=1, pattern=r'\S', description='平台用途说明。')
    # 静态展示标记完全取自中间件，不由在线状态、SQL 管理员或当前用户权限推断。
    supports_a2a: bool = Field(description='中间件配置声明的平台是否参与 A2A 协议，不代表通信能力已就绪。')
    is_sub_management_platform: bool = Field(description='中间件配置声明的是否为子管理平台，不授予用户管理权限。')
    is_management_entry: bool = Field(description='中间件配置声明的是否作为管理平台入口，与子管理平台标记独立。')
    is_alive: bool = Field(description='中间件平台会话是否存活，不代表业务健康；离线平台仍在列表中。')


class MiddlewarePlatformInfo(PlatformInfo):
    """完整上游响应；A2A职责供内部使用，不增加当前平台列表API字段。"""

    a2aresponsibility: str = Field(description='平台在A2A协作中的职责；子管理平台须非空，其他平台未配置时为空字符串。')

    @model_validator(mode='after')
    def require_sub_platform_responsibility(self):
        # 对齐中间件配置边界，不能用supports_a2a=false绕过子管理平台职责声明。
        if self.is_sub_management_platform and not self.a2aresponsibility.strip():
            raise ValueError('子管理平台的A2A职责不能为空')
        return self
