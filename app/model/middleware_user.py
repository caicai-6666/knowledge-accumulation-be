"""中央用户资料与登录状态的严格出站 DTO，不包含平台或用户 token。"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator

from app.model.personnel import PlatformCode, EmploymentStatus


def reject_duplicate_departments(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError('部门名称不能重复')
    return values


DepartmentName = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1),
    Field(description='部门名称，去除首尾空白后非空；不包含部门 ID 或层级。')]
Departments = Annotated[list[DepartmentName], Field(strict=True, json_schema_extra={'uniqueItems': True}),
    AfterValidator(reject_duplicate_departments)]


class MiddlewareUserInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    userid: str = Field(strict=True, min_length=1, max_length=128, pattern=r'\S', description='员工 userid。')
    username: str = Field(strict=True, min_length=1, max_length=200, pattern=r'\S', description='员工姓名，历史未知时使用 userid。')
    permissions: dict[PlatformCode, Annotated[int, Field(strict=True, ge=0)]] = Field(description='平台代码到非负文件访问等级的映射，不携带管理权限。')
    # 全表省略部门会清空中央资料，本地强制调用方明确提供，避免扩展 DTO 时静默漏传。
    departments: Departments = Field(description='全部所属部门名称，未知时传 []；去除首尾空白后不得重复，顺序不表示主部门或权限。')
    # 中央全表省略状态会重置为在职，调用方必须显式提供权威状态。
    employment_status: EmploymentStatus = Field(description='员工任职状态：employed 在职，resigned 离职；从权威员工资料原样同步，不过滤离职员工。')


class MiddlewareUserEmploymentStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    userid: str = Field(strict=True, min_length=1, max_length=128, description='本次全表成功保存的员工 userid。')
    employment_status: EmploymentStatus = Field(description='本次成功保存的任职状态，须与对应请求一致。')


class MiddlewareUserTableResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    count: int = Field(strict=True, ge=0, description='中间件确认的总表用户数量，必须等于提交数量。')
    users: list[MiddlewareUserEmploymentStatus] = Field(description='按请求顺序返回的逐用户任职状态回执；空总表为 []。')


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
    departments: Departments | None = Field(default=None,
        description='有效登录的部门名称列表，无部门为 []；无效登录为 null，旧版本响应可省略。')
    employment_status: EmploymentStatus | None = Field(default=None,
        description='有效登录时为当前 employed 或 resigned，无效登录时为 null；旧版本响应可省略，不据此改变登录或权限。')

    @model_validator(mode='after')
    def validate_username(self):
        if self.username is not None and (not self.username.strip() or not self.is_logged_in):
            raise ValueError('中央用户名与登录状态不一致')
        if 'departments' in self.model_fields_set and ((self.departments is not None) != self.is_logged_in):
            raise ValueError('中央部门列表与登录状态不一致')
        if 'employment_status' in self.model_fields_set and ((self.employment_status is not None) != self.is_logged_in):
            raise ValueError('中央任职状态与登录状态不一致')
        return self
