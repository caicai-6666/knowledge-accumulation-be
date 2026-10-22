"""面向用户的平台基础信息与数据库管理员展示资料。"""

from pydantic import Field

from app.model.platform import PlatformInfo
from app.model.platform_admin import PlatformAdminProfile


class PlatformResponse(PlatformInfo):
    admins: list[PlatformAdminProfile] = Field(default_factory=list,
        description='从权威数据库读取的管理员姓名与头像文件名；无管理员为空数组，不代表当前调用者的管理授权。')
