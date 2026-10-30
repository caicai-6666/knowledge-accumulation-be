"""启动按用户→管理员顺序全表注册，首次登录只补用户登记；full-table 可能删除遗漏用户及其登录缓存。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx

from app.auth.model import AuthenticatedUser
from app.config.middleware_user import MiddlewareUserSyncSettings
from app.infrastructure.middleware import MiddlewareClient
from app.model.middleware_user import MiddlewareUserInfo
from app.model.platform_admin import PlatformAdminTable
from app.service.middleware_session import MiddlewareSessionService

logger = logging.getLogger(__name__)


class EmployeeReader(Protocol):
    async def list_users(self) -> list[AuthenticatedUser]: ...

    async def list_platform_admins(self) -> dict[str, list[str]]: ...

    async def find_by_userid(self, userid: str) -> AuthenticatedUser | None: ...


def middleware_profile(user: AuthenticatedUser) -> MiddlewareUserInfo:
    # 中央契约只接收名称；按部门 ID 稳定排序后去重同名，不改写本地多部门身份。
    # 仅旧身份/未落库模拟用户缺少完整列表时，回退已有单部门名称。
    names = ([item.name for item in sorted(user.departments, key=lambda item: item.id)]
             if user.departments else [user.department_name] if user.department_name is not None else [])
    departments = list(dict.fromkeys(name.strip() for name in names))
    return MiddlewareUserInfo(userid=user.userid, username=user.name or user.userid,
                              permissions=user.platform_permissions, departments=departments)


class MiddlewareUserSyncService:
    def __init__(
        self, settings: MiddlewareUserSyncSettings, employees: EmployeeReader,
        sessions: MiddlewareSessionService, client: MiddlewareClient,
        *, maintenance_lock: asyncio.Lock, simulated_user: AuthenticatedUser | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._settings, self._employees = settings, employees
        self._sessions, self._client = sessions, client
        self._simulated_user, self._sleep = simulated_user, sleep
        self._maintenance_lock = maintenance_lock
        self._task: asyncio.Task | None = None
        self._closed = False
        self._initialized = False
        self._admins_initialized = False
        self._registered_userids: frozenset[str] = frozenset()
        self._developer_in_sql = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def startup_complete(self) -> bool:
        """启动阶段两张表均成功才算完成；用户登记确认仍与管理员发布状态独立。"""
        return self._initialized and self._admins_initialized

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError('启动用户同步服务已关闭')
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='middleware-user-initial-sync')

    def forget_registration(self, userid: str) -> None:
        """中央明确报告资料缺失时，仅丢弃本地确认记录，不自动恢复中央资料。"""
        self._registered_userids = self._registered_userids - {userid}

    async def ensure_registered_locked(self, userid: str) -> None:
        """仅显式登录持有组合根维护锁时调用；成功批次已包含该用户则不重复发全表。"""
        if self._closed:
            raise RuntimeError('用户注册服务已关闭')
        if userid in self._registered_userids:
            return
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            await self._publish_locked(required_userid=userid)

    async def _publish_locked(self, *, required_userid: str | None = None) -> None:
        # 调用方必须持有唯一维护锁。锁只协调本进程，不提供与 registry 编辑的跨后端一致性。
        if self._closed:
            raise RuntimeError('用户注册服务已关闭')
        token = self._sessions.get_access_token()
        if token is None:
            raise RuntimeError('中间件平台会话暂不可用')
        try:
            # 读取权威完整范围的现有权限，不按登录用户对象或目录重新生成已有员工等级。
            users = list(await self._employees.list_users())
            if len({user.userid for user in users}) != len(users):
                raise ValueError('员工总表存在重复 userid')
            developer_in_sql = self._simulated_user is not None and any(
                user.userid == self._simulated_user.userid for user in users)
            # 配置的开发 userid 可以显式绑定 SQL 员工；已有资料完全由数据库提供，禁止配置覆盖。
            if self._simulated_user is not None and not developer_in_sql:
                users.append(self._simulated_user)
            ids = frozenset(user.userid for user in users)
            if required_userid is not None and required_userid not in ids:
                raise ValueError('权威总表未包含待登记用户，拒绝发布不完整注册资料')
            profiles = [middleware_profile(user) for user in users]
            count = await self._client.replace_user_table(profiles, token)
            if type(count) is not int or count != len(profiles):
                raise ValueError('用户注册响应数量与提交数量不一致')
            # 仅保存已确认成员集合，不维护可与 registry 分叉的本地权限缓存。
            self._registered_userids = ids
            self._developer_in_sql = developer_in_sql
            self._initialized = True
            logger.info('用户全表注册成功：count=%s', count)
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                self._sessions.invalidate_access_token(token)
            raise

    async def _publish_admins_locked(self) -> None:
        """仅启动任务持同一维护锁时调用，先确认用户登记，再发布权威关联及独立模拟身份。"""
        token = self._sessions.get_access_token()
        if token is None:
            raise RuntimeError('中间件平台会话暂不可用')
        try:
            # SQL 故障必须上抛；不能用员工展示字段或空对象替代权威管理员关联查询。
            table = PlatformAdminTable(admins=await self._employees.list_platform_admins())
            admins = {code: list(ids) for code, ids in table.admins.items()}
            if self._simulated_user is not None:
                simulated = self._simulated_user
                employee = await self._employees.find_by_userid(simulated.userid)
                if (employee is not None) != self._developer_in_sql:
                    raise ValueError('开发身份数据源在两阶段之间改变，需重新核对用户注册')
                if employee is not None and employee.userid != simulated.userid:
                    raise ValueError('员工查询身份与开发 userid 不一致')
                if not self._developer_in_sql:
                    if any(simulated.userid in ids for ids in admins.values()):
                        raise ValueError('SQL 管理员 userid 与未落库模拟身份冲突')
                    for code in simulated.managed_platforms:
                        admins.setdefault(code, []).append(simulated.userid)
            table = PlatformAdminTable(admins=admins)
            assigned = {userid for ids in table.admins.values() for userid in ids}
            if not assigned <= self._registered_userids:
                # 不因管理员失败反复覆盖用户表；新增人员先由既有显式注册机制完成登记。
                raise ValueError('管理员关联包含尚未完成用户全表登记的 userid')
            count = await self._client.replace_platform_admin_table(table.admins, token)
            if type(count) is not int or count != table.relation_count:
                raise ValueError('管理员全表响应关系数与提交数量不一致')
            self._admins_initialized = True
            logger.info('管理员全表启动同步成功：admin_count=%s', count)
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                self._sessions.invalidate_access_token(token)
            raise

    async def _run(self) -> None:
        while True:
            if self._sessions.get_access_token() is None:
                await self._sleep(min(1, self._settings.retry_interval_seconds))
                continue
            try:
                # 启动注册与显式首次登记共享临界区，锁等待也计入本次总超时。
                async with asyncio.timeout(self._settings.operation_timeout_seconds), self._maintenance_lock:
                    if self.startup_complete:
                        return
                    if not self._initialized:
                        await self._publish_locked()
                    if not self._admins_initialized:
                        await self._publish_admins_locked()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                logger.warning('启动用户或管理员全表注册失败，将重试：error_type=%s status=%s', type(exc).__name__, status)
            await self._sleep(self._settings.retry_interval_seconds)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        # 客户端和仓库由组合根关闭，停机不向中间件发布任何清理请求。
