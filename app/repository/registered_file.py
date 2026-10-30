"""注册中心正式文件只读适配；权限与文件在同一权威数据库快照读取。"""
import asyncio
from dataclasses import dataclass
import json
import asyncmy
from asyncmy.cursors import DictCursor
from pydantic import TypeAdapter
from app.auth.model import AuthenticatedUser
from app.config.file_retrieval import load_file_source_settings


@dataclass(frozen=True)
class RegisteredFile:
    id: str
    name: str | None
    abstract: str | None
    platform: str
    level: int
    passport: tuple[str, ...] | None = None
    source_id: str | None = None


def decode_passports(value):
    """保留权威数组的顺序、重复值与原文；NULL 与已知空数组分开。"""
    if value is None:
        return None
    if isinstance(value,(str,bytes)):
        value=json.loads(value)
    if not isinstance(value,list) or any(not isinstance(item,str) or not item.strip() or len(item)>512 for item in value):
        raise ValueError('正式文件通行证字段不合法')
    return tuple(value)


class RegisteredFileRepository:
    def __init__(self, settings=None):
        self.settings = settings
        self._pool = None
        self._lock = asyncio.Lock()
        self.closed = False

    async def _initialize(self):
        async with self._lock:
            if self.closed:
                raise RuntimeError('文件来源已关闭')
            if self._pool is None:
                self.settings = self.settings or load_file_source_settings()
                s = self.settings
                async with asyncio.timeout(s.operation_timeout_seconds):
                    self._pool = await asyncmy.create_pool(host=s.host, port=s.port, db=s.database,
                        user=s.user, password=s.password.get_secret_value(), charset='utf8mb4',
                        autocommit=False, minsize=1, maxsize=3, connect_timeout=s.connect_timeout_seconds,
                        pool_recycle=300)

    async def read(self, userid, ids=None, platforms=None, *, passport=None):
        if passport is not None and (not isinstance(passport,str) or not passport.strip() or len(passport)>512):
            raise ValueError('通行证须为非空白且最多512字符的字符串')
        if ids is not None and not ids:
            return ()
        await self._initialize()
        async with asyncio.timeout(self.settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
                # READ ONLY 防止适配器意外写入注册中心；不建立/修改任何源表或索引。
                await cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                await cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                try:
                    await cursor.execute('SELECT platform_permissions FROM employees WHERE CAST(userid AS BINARY)=CAST(%s AS BINARY)', (userid,))
                    employee = await cursor.fetchone()
                    if employee is None:
                        return ()
                    value = employee['platform_permissions']
                    permissions = TypeAdapter(AuthenticatedUser.model_fields['platform_permissions'].annotation).validate_python(
                        json.loads(value) if isinstance(value, (str, bytes)) else value)
                    allowed = [(p, level) for p, level in permissions.items() if platforms is None or p in platforms]
                    if not allowed:
                        return ()
                    clauses = ['file_level IS NOT NULL', '('+' OR '.join('(source_platform=%s AND file_level<=%s)' for _ in allowed)+')']
                    params = [item for pair in allowed for item in pair]
                    if passport is not None:
                        # JSON 字符串成员精确匹配；保留大小写、空格及逗号，不解释为模糊或多值查询。
                        clauses.append('JSON_CONTAINS(passport, %s) = 1')
                        params.append(json.dumps(passport,ensure_ascii=False))
                    if ids is not None:
                        clauses.append('id IN ('+','.join('%s' for _ in ids)+')')
                        params.extend(ids)
                    await cursor.execute('SELECT id,name,abstract,source_platform,file_level,passport,source_id FROM resources WHERE '+
                        ' AND '.join(clauses)+' ORDER BY id', params)
                    rows = await cursor.fetchall()
                    result = []
                    for row in rows:
                        if (type(row['id']) is not int or row['id'] < 1 or type(row['file_level']) is not int
                            or row['source_platform'] not in permissions or permissions[row['source_platform']] < row['file_level']
                            or any(row[k] is not None and not isinstance(row[k], str) for k in ('name','abstract','source_id'))):
                            raise ValueError('正式文件字段不合法')
                        result.append(RegisteredFile(str(row['id']),row['name'],row['abstract'],row['source_platform'],row['file_level'],
                            decode_passports(row['passport']), source_id=row['source_id']))
                    return tuple(result)
                finally:
                    await connection.rollback()

    async def close(self):
        async with self._lock:
            self.closed = True
            if self._pool is not None:
                self._pool.close()
                await self._pool.wait_closed()
                self._pool = None
