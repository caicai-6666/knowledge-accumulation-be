"""首次钉钉登录：解析顶级部门、持久化头像并原子初始化零权限员工。"""

import asyncio
import logging
from weakref import WeakValueDictionary

from app.auth.error import AuthUnavailable
from app.auth.model import AuthenticatedUser
from app.infrastructure.avatar import AvatarStore, avatar_response_filename
from app.infrastructure.dingtalk import DingTalkClient
from app.model.dingtalk import DingTalkDepartment
from app.repository.employee import MySQLEmployeeRepository
from app.service.platform_directory import PlatformDirectoryService

logger = logging.getLogger(__name__)


class EmployeeOnboardingService:
    def __init__(self, client: DingTalkClient, employees: MySQLEmployeeRepository, avatars: AvatarStore,
                 platforms: PlatformDirectoryService, *, own_platform_code: str, timeout_seconds: float = 60):
        self._client = client
        self._employees = employees
        self._avatars = avatars
        self._platforms = platforms
        self._own_platform_code = own_platform_code
        self._timeout_seconds = timeout_seconds
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def _top_department(self, department_ids: list[int]) -> DingTalkDepartment:
        cache = {}
        top_departments = {}
        for department_id in dict.fromkeys(department_ids):
            visited = set()
            current = department_id
            for _ in range(32):
                if current in visited:
                    raise AuthUnavailable('钉钉部门层级存在循环，无法确认顶级部门')
                visited.add(current)
                if current not in cache:
                    cache[current] = await self._client.get_department(current)
                department = cache[current]
                if department.dept_id == 1 or department.parent_id == 1:
                    top_departments[department.dept_id] = department
                    break
                if department.parent_id == 0:
                    raise AuthUnavailable('钉钉部门层级未连接到企业根部门')
                current = department.parent_id
            else:
                raise AuthUnavailable('钉钉部门层级超出限制')
        # 根部门代表企业本身；同时返回实际部门时，以根部门下的一级部门为准。
        if len(top_departments) > 1:
            top_departments.pop(1, None)
        if len(top_departments) != 1:
            raise AuthUnavailable('员工归属多个顶级部门，尚无法确定唯一部门')
        return next(iter(top_departments.values()))

    async def _zero_permissions(self) -> dict[str, int]:
        platforms = await self._platforms.list_platforms()
        # 保留目录返回的离线平台，并补入自身；隐藏平台的可见性遵循中间件目录契约。
        codes = {self._own_platform_code, *(platform.code for platform in platforms)}
        return {code: 0 for code in sorted(codes)}

    async def _cleanup_attempt(self, userid: str, relative_url: str, *, known_user: AuthenticatedUser | None = None):
        try:
            # 提交异常可能实际已成功；重新确认后才删除，数据库不可读时保留文件供核对。
            user = known_user if known_user is not None else await self._employees.find_by_userid(userid)
            if user is None or user.avatar_url != avatar_response_filename(relative_url):
                await self._avatars.delete(relative_url)
        except Exception as error:
            logger.warning('初始化头像保留待核对：error_type=%s', type(error).__name__)

    async def initialize(self, userid: str) -> AuthenticatedUser:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._initialize_locked(userid)
        except TimeoutError:
            raise AuthUnavailable('员工资料初始化超时，请重新获取钉钉免登 code 后重试') from None

    async def _initialize_locked(self, userid: str) -> AuthenticatedUser:
        lock = self._locks.get(userid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[userid] = lock
        async with lock:
            relative_url = None
            try:
                # 同进程并发免登只执行一次外部资料读取；跨进程仍由 MySQL 主键保护。
                existing = await self._employees.find_by_userid(userid)
                if existing is not None:
                    return existing
                profile = await self._client.get_employee(userid)
                if profile.userid != userid:
                    raise AuthUnavailable('员工资料与钉钉免登身份不一致')
                department = await self._top_department(profile.dept_id_list)
                permissions = await self._zero_permissions()
                if profile.avatar:
                    relative_url = await self._avatars.save_from_url(profile.avatar)
                user = await self._employees.initialize_employee(userid=userid, name=profile.name,
                    department=department, avatar_url=relative_url, platform_permissions=permissions)
                if relative_url is not None:
                    await self._cleanup_attempt(userid, relative_url, known_user=user)
                return user
            except BaseException as error:
                if relative_url is not None:
                    await asyncio.shield(self._cleanup_attempt(userid, relative_url))
                if isinstance(error, (AuthUnavailable, asyncio.CancelledError)):
                    raise
                if not isinstance(error, Exception):
                    raise
                logger.warning('首次登录员工初始化失败：error_type=%s', type(error).__name__)
                raise AuthUnavailable('员工资料初始化暂不可用，请重新获取钉钉免登 code 后重试') from None
