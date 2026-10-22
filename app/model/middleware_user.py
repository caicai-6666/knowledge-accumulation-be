"""中央用户资料与登录状态的严格出站 DTO，不包含平台或用户 token。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.model.personnel import PlatformCode


class MiddlewareUserInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'\S', description='员工 userid。')
    username: str = Field(strict=True, min_length=1, max_length=200, pattern=r'\S', description='员工姓名，历史未知时使用 userid。')
    permissions: dict[PlatformCode, Annotated[int, Field(strict=True, ge=0)]] = Field(description='平台代码到非负文件访问等级的映射，不携带管理权限。')


class MiddlewareUserTableResult(BaseModel):
    count: int = Field(strict=True, ge=0, description='中间件确认的总表用户数量，必须等于提交数量。')


class MiddlewareUserLoginStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    userid: str | None = Field(description='有效用户 token 对应的可信 userid；离线或无效 token 为 null。', strict=True, min_length=1, max_length=128)
    is_logged_in: StrictBool = Field(description='是否已有尚未过期的中央登录。')
    ttl: int = Field(strict=True, ge=0, description='中央登录剩余秒数，离线时为 0。')

    @model_validator(mode='after')
    def validate_ttl(self):
        if self.userid is not None and (not self.userid.strip() or self.userid != self.userid.strip()):
            raise ValueError('中央 userid 不合法')
        if self.is_logged_in != (self.ttl > 0) or self.is_logged_in != (self.userid is not None):
            raise ValueError('中央登录状态与剩余有效期不一致')
        return self


class MiddlewareUserLoginQueryStatus(MiddlewareUserLoginStatus):
    # POST 新增展示名称；保留缺省值兼容尚未升级的中间件，PUT 仍沿用原响应。
    username: str | None = Field(default=None, strict=True, min_length=1,
        description='有效中央登录对应的当前用户名；无效登录为 null，旧版本响应可省略。')

    @model_validator(mode='after')
    def validate_username(self):
        if self.username is not None and (not self.username.strip() or not self.is_logged_in):
            raise ValueError('中央用户名与登录状态不一致')
        return self
