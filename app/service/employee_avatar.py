"""补齐现有员工头像，数据库条件更新裁决并发，未采用的候选文件及时清理。"""

import asyncio
import logging
import httpx
from PIL import UnidentifiedImageError, Image

from app.infrastructure.avatar import avatar_response_filename
from app.schema.employee_avatar import FillEmployeeAvatarResponse

logger = logging.getLogger(__name__)


class AvatarInputError(ValueError):
    pass


class AvatarEmployeeNotFound(LookupError):
    pass


class EmployeeAvatarService:
    def __init__(self, employees, avatars):
        self.employees, self.avatars = employees, avatars

    async def require_registry_admin(self, userid):
        # 再查SQL权威关系，不以文件等级或开发模拟身份里的权限代替管理授权。
        user = await self.employees.find_by_userid(userid)
        return user is not None and user.userid == userid and 'registry' in user.managed_platforms

    async def _cleanup(self, userid, candidate):
        try:
            # commit报错可能实际提交成功；无法回读确认时保留文件，不能破坏已提交头像。
            _, current = await self.employees.find_avatar(userid)
            if current != candidate:
                await self.avatars.delete(candidate)
        except Exception as error:
            logger.warning('补齐头像文件保留待核对：error_type=%s', type(error).__name__)

    async def fill(self, userid, url):
        exists, current = await self.employees.find_avatar(userid)
        if not exists:
            raise AvatarEmployeeNotFound()
        if current:
            return FillEmployeeAvatarResponse(userid=userid, status='existing',
                avatar_url=avatar_response_filename(current))
        if not url:
            return FillEmployeeAvatarResponse(userid=userid, status='skipped', avatar_url=None)
        try:
            candidate = await self.avatars.save_from_url(url)
        except (ValueError, httpx.InvalidURL, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            # 图片格式和URL失败不回显远程地址或图片原始异常。
            raise AvatarInputError('头像地址或图片内容不符合允许域名、格式及大小要求') from None
        try:
            changed, current = await self.employees.fill_avatar(userid, candidate)
        except BaseException:
            # 请求取消时也等待清理核验，不留下无人等待的文件操作任务。
            cleanup = asyncio.create_task(self._cleanup(userid, candidate))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise
        if current != candidate:
            await self.avatars.delete(candidate)
        if current is None:
            raise AvatarEmployeeNotFound()
        return FillEmployeeAvatarResponse(userid=userid, status='created' if changed else 'existing',
            avatar_url=avatar_response_filename(current))
