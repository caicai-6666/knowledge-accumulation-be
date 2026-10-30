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
from app.model.personnel import Department, PlatformAdmin, EmployeeName, EmployeeWorkProfile


_DEPARTMENTS_SELECT = (
    "COALESCE((SELECT JSON_ARRAYAGG(JSON_OBJECT('id', dep.id, 'name', dep.name)) FROM departments dep "
    'WHERE JSON_CONTAINS(COALESCE(e.department_ids,JSON_ARRAY(e.department_id)),CAST(dep.id AS JSON))),JSON_ARRAY()) AS departments'
)
_EMPLOYEE_SELECT = (
    'SELECT e.userid,e.name,e.avatar_url,e.department_id,e.platform_permissions,d.name AS department_name, '
    + _DEPARTMENTS_SELECT + ', '
    # 同一条查询读取员工与管理员关联，避免文件权限等级被误用为管理身份。
    'COALESCE((SELECT JSON_ARRAYAGG(a.platform_code) FROM platform_admins AS a '
    'WHERE a.employee_userid=e.userid),JSON_ARRAY()) AS managed_platforms '
    'FROM employees AS e JOIN departments AS d ON d.id=e.department_id'
)
_EMPLOYEE_QUERY = _EMPLOYEE_SELECT + ' WHERE e.userid=%s'


def _department_directory_query(username, department, limit, offset):
    """SQL 内完成两路召回和融合，最后按用户限量，避免提前截断遗漏双路命中者。"""
    def literal(value):
        return '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'

    # 每位员工在部门路只有一次贡献，取最好的名称匹配和全文分值，不能按部门数累加。
    department_matches = (
        'SELECT e.userid, MAX(LOWER(dep.name)=LOWER(%s)) AS exact_match, '
        'MAX(MATCH(dep.name) AGAINST(%s IN NATURAL LANGUAGE MODE)) AS relevance '
        'FROM departments AS dep JOIN employees AS e ON '
        'JSON_CONTAINS(COALESCE(e.department_ids,JSON_ARRAY(e.department_id)),CAST(dep.id AS JSON)) '
        'WHERE MATCH(dep.name) AGAINST(%s IN NATURAL LANGUAGE MODE)>0 '
        "OR LOWER(dep.name) LIKE LOWER(%s) ESCAPE '!' GROUP BY e.userid"
    )
    args = (department, department, department, literal(department))
    if username is None:
        prefix = 'WITH department_matches AS (' + department_matches + ') '
        source = 'department_matches AS matched'
        order = 'matched.exact_match DESC,matched.relevance DESC,username,e.userid'
    else:
        name_matches = (
            'SELECT e.userid, (LOWER(e.name)=LOWER(%s)) AS exact_match, '
            'MATCH(e.name) AGAINST(%s IN NATURAL LANGUAGE MODE) AS relevance FROM employees AS e '
            'WHERE MATCH(e.name) AGAINST(%s IN NATURAL LANGUAGE MODE)>0 '
            "OR LOWER(e.name) LIKE LOWER(%s) ESCAPE '!'"
        )
        # 与文件检索 union_candidates 一致：等权 RRF(k=60)，同分共享名次，
        # 一路为空时其贡献自然为零；不混加不同字段的全文分值，也不把 ID 排序当相关性。
        prefix = ('WITH name_matches AS (' + name_matches + '), department_matches AS (' + department_matches + '), '
            'route_ranks AS ('
            'SELECT userid,RANK() OVER (ORDER BY exact_match DESC,relevance DESC) AS match_rank FROM name_matches '
            'UNION ALL '
            'SELECT userid,RANK() OVER (ORDER BY exact_match DESC,relevance DESC) AS match_rank FROM department_matches), '
            'fused AS (SELECT userid,SUM(CAST(0.5 AS DOUBLE)/(60+match_rank)) AS relevance FROM route_ranks GROUP BY userid) ')
        source = 'fused AS matched'
        order = ('matched.relevance DESC,CASE WHEN EXISTS(SELECT 1 FROM name_matches) '
                 'AND EXISTS(SELECT 1 FROM department_matches) THEN e.userid '
                 'ELSE COALESCE(e.name,e.userid) END,e.userid')
        args = (username, username, username, literal(username), *args)
    query = (prefix + 'SELECT e.userid,COALESCE(e.name,e.userid) AS username, ' + _DEPARTMENTS_SELECT
        + ' FROM employees AS e JOIN ' + source + ' ON matched.userid=e.userid ORDER BY ' + order
        + ' LIMIT %s OFFSET %s')
    return query, (*args, limit, offset)


def _json_value(value):
    # 驱动或测试适配可返回 JSON 字符串或已解码对象，非法 JSON 必须失败。
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _user(row: dict) -> AuthenticatedUser:
    return AuthenticatedUser(userid=row['userid'], name=row['name'],
        avatar_url=avatar_response_filename(row['avatar_url']), department_id=row['department_id'],
        department_name=row['department_name'], platform_permissions=_json_value(row['platform_permissions']),
        managed_platforms=_json_value(row['managed_platforms']),
        departments=sorted(_json_value(row['departments']), key=lambda item:item['id']))


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

    async def find_work_profile(self, userid: str) -> EmployeeWorkProfile | None:
        """按精确 userid 读取任职资料；不进入中央同步或公开认证响应。"""
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                await cursor.execute('SELECT work_profile FROM employees WHERE userid=%s', (userid,))
                row = await cursor.fetchone()
        if row is None:
            return None
        return EmployeeWorkProfile.model_validate(_json_value(row['work_profile']))

    async def search_user_names(self, username: str | None = None, *, department: str | None = None,
                               offset: int = 0, limit: int = 11) -> list[EmployeeName]:
        """姓名与部门独立召回后取并集；空条件跳过，两个条件均为空时浏览全体。"""
        for value in (username, department):
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise ValueError('姓名或部门查询条件不合法')
        username = (username.strip() or None) if username is not None else None
        department = (department.strip() or None) if department is not None else None
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError('人员目录分页参数不合法')
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                if department is not None:
                    query, args = _department_directory_query(username, department, limit, offset)
                    await cursor.execute(query, args)
                elif username is None:
                    # 显式的无筛选查询，不用通配符模拟全量，也不依赖全文索引；仍受数量上限约束。
                    await cursor.execute('SELECT e.userid,COALESCE(e.name,e.userid) AS username, '
                        + _DEPARTMENTS_SELECT + ' FROM employees AS e '
                        'ORDER BY username,e.userid LIMIT %s OFFSET %s', (limit, offset))
                else:
                    # ngram 全文召回不要求连续包含查询词；LIKE 补充单字/停用词。
                    # 完整姓名优先，随后按全文相关性和稳定身份排序；分数不进入目录 DTO。
                    keyword = username.strip()
                    # 显式转义，姓名中的 %、_ 按字面匹配；值始终作为绑定参数。
                    text = keyword.replace('!', '!!').replace('%', '!%').replace('_', '!_')
                    await cursor.execute("SELECT e.userid,e.name AS username, "
                        + _DEPARTMENTS_SELECT + ' FROM employees AS e '
                        "WHERE MATCH(e.name) AGAINST(%s IN NATURAL LANGUAGE MODE)>0 "
                        "OR LOWER(e.name) LIKE LOWER(%s) ESCAPE '!' "
                        "ORDER BY (LOWER(e.name)=LOWER(%s)) DESC, "
                        "MATCH(e.name) AGAINST(%s IN NATURAL LANGUAGE MODE) DESC, "
                        'e.name,e.userid LIMIT %s OFFSET %s',
                        (keyword, '%' + text + '%', keyword, keyword, limit, offset))
                rows = await cursor.fetchall()
        return [EmployeeName.model_validate({**row, 'departments': _json_value(row['departments'])}) for row in rows]

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

    async def initialize_employee(self, *, userid: str, name: str, departments: list[DingTalkDepartment],
                                  avatar_url: str | None, platform_permissions: dict[str, int],
                                  work_profile: EmployeeWorkProfile | None = None) -> AuthenticatedUser:
        # 全部一级部门同事务保存；单值仅作旧接口兼容，不赋予主部门语义。
        departments = sorted({item.dept_id:item for item in departments}.values(), key=lambda item:item.dept_id)
        if not departments:
            raise ValueError('员工至少需要一个部门')
        department_ids = [item.dept_id for item in departments]
        avatar_response_filename(avatar_url)
        profile = EmployeeWorkProfile.model_validate(
            work_profile.model_dump() if work_profile is not None else {})
        await self._initialize()
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        for department in departments:
                            await cursor.execute(
                                'INSERT INTO departments(id,name) VALUES(%s,%s) AS incoming '
                                'ON DUPLICATE KEY UPDATE name=incoming.name', (department.dept_id, department.name))
                        # 主键是并发初始化的最终仲裁：已有员工不改姓名、头像、部门或权限。
                        await cursor.execute(
                            'INSERT INTO employees(userid,name,avatar_url,department_id,platform_permissions,work_profile,department_ids) '
                            'VALUES(%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE userid=employees.userid',
                            (userid, name, avatar_url, department_ids[0],
                             json.dumps(platform_permissions, ensure_ascii=False, separators=(',', ':')),
                             profile.model_dump_json(), json.dumps(department_ids)))
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
