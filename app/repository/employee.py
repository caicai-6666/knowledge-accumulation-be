"""员工查询与首次登录事务，不创建表，不覆盖已有员工权限。"""

import asyncio
import json

import asyncmy
from asyncmy.cursors import DictCursor

from app.auth.model import AuthenticatedUser
from app.config.database import MySQLSettings, load_mysql_settings
from app.infrastructure.avatar import avatar_response_filename
from app.model.dingtalk import DingTalkDepartment
from app.model.platform_admin import PlatformAdminProfile
from app.model.personnel import Department, PlatformAdmin


_EMPLOYEE_SELECT = (
    'SELECT e.userid,e.name,e.avatar_url,e.department_id,e.platform_permissions,d.name AS department_name, '
    # 同一条查询读取员工与管理员关联，避免文件权限等级被误用为管理身份。
    'COALESCE((SELECT JSON_ARRAYAGG(a.platform_code) FROM platform_admins AS a '
    'WHERE a.employee_userid=e.userid),JSON_ARRAY()) AS managed_platforms '
    'FROM employees AS e JOIN departments AS d ON d.id=e.department_id'
)
_EMPLOYEE_QUERY = _EMPLOYEE_SELECT + ' WHERE e.userid=%s'


def _json_value(value):
    # 驱动或测试适配可返回 JSON 字符串或已解码对象，非法 JSON 必须失败。
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _user(row: dict) -> AuthenticatedUser:
    return AuthenticatedUser(userid=row['userid'], name=row['name'],
        avatar_url=avatar_response_filename(row['avatar_url']), department_id=row['department_id'],
        department_name=row['department_name'], platform_permissions=_json_value(row['platform_permissions']),
        managed_platforms=_json_value(row['managed_platforms']))


class MySQLEmployeeRepository:
    def __init__(self, settings: MySQLSettings | None = None):
        self._settings = settings
        self._pool: asyncmy.Pool | None = None
        self._lock = asyncio.Lock()

    async def _initialize(self) -> None:
        async with self._lock:
            if self._pool is not None:
                return
            settings = self._settings or load_mysql_settings()
            self._settings = settings
            async with asyncio.timeout(settings.operation_timeout_seconds):
                self._pool = await asyncmy.create_pool(
                    host=settings.host, port=settings.port, user=settings.user,
                    password=settings.password.get_secret_value(), db=settings.database,
                    charset="utf8mb4", autocommit=True, minsize=1, maxsize=5,
                    connect_timeout=settings.connect_timeout_seconds, pool_recycle=300,
                )

    async def list_departments(self) -> list[Department]:
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                await cursor.execute('SELECT id,name FROM departments ORDER BY id')
                rows = await cursor.fetchall()
        return [Department.model_validate(row) for row in rows]

    async def find_by_userid(self, userid: str) -> AuthenticatedUser | None:
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                await cursor.execute(_EMPLOYEE_QUERY, (userid,))
                row = await cursor.fetchone()
        if row is None:
            return None
        return _user(row)

    async def list_users(self) -> list[AuthenticatedUser]:
        """读取权威员工总表；查询失败必须上抛，不能作为空表同步。"""
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                await cursor.execute(_EMPLOYEE_SELECT + ' ORDER BY e.userid')
                rows = await cursor.fetchall()
        return [_user(row) for row in rows]

    async def list_platform_admins(self) -> dict[str, list[str]]:
        """读取全部管理员关联；失败上抛，禁止把数据库故障作为空总表发布。"""
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                await cursor.execute('SELECT platform_code,employee_userid FROM platform_admins '
                                     'ORDER BY platform_code,employee_userid')
                rows = await cursor.fetchall()
        admins: dict[str, list[str]] = {}
        for row in rows:
            relation = PlatformAdmin.model_validate(row)
            admins.setdefault(relation.platform_code, []).append(relation.employee_userid)
        return admins

    async def list_platform_admin_profiles(self) -> dict[str, list[PlatformAdminProfile]]:
        """批量读取管理员名称与头像，头像仅返回经过校验的文件名。"""
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                await cursor.execute('SELECT a.platform_code,e.userid,e.name,e.avatar_url FROM platform_admins AS a '
                                     'JOIN employees AS e ON e.userid=a.employee_userid '
                                     'ORDER BY a.platform_code,e.userid')
                rows = await cursor.fetchall()
        profiles: dict[str, list[PlatformAdminProfile]] = {}
        for row in rows:
            profiles.setdefault(row['platform_code'], []).append(PlatformAdminProfile(
                name=row['name'] or row['userid'], avatar_url=avatar_response_filename(row['avatar_url'])))
        return profiles

    async def initialize_employee(self, *, userid: str, name: str, department: DingTalkDepartment,
                                  avatar_url: str | None, platform_permissions: dict[str, int]) -> AuthenticatedUser:
        avatar_response_filename(avatar_url)
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            'INSERT INTO departments(id,name) VALUES(%s,%s) AS incoming '
                            'ON DUPLICATE KEY UPDATE name=incoming.name', (department.dept_id, department.name))
                        # 主键是并发初始化的最终仲裁：已有员工不改姓名、头像、部门或权限。
                        await cursor.execute(
                            'INSERT INTO employees(userid,name,avatar_url,department_id,platform_permissions) '
                            'VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE userid=employees.userid',
                            (userid, name, avatar_url, department.dept_id,
                             json.dumps(platform_permissions, ensure_ascii=False, separators=(',', ':'))))
                        await cursor.execute(_EMPLOYEE_QUERY, (userid,))
                        user = _user(await cursor.fetchone())
                    await connection.commit()
                    return user
                except BaseException:
                    await connection.rollback()
                    raise

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
