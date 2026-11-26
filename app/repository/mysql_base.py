"""轻量化 MySQL 异步仓库基类：共享连接池、事务上下文、命名锁及基础编解码辅助。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json

import asyncmy
from asyncmy.cursors import DictCursor

from app.config.database import MySQLSettings, load_mysql_settings


def _decode(value):
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _row(model, row):
    # MySQL DATETIME 不携带时区；本仓库统一按 UTC 写入和恢复。
    values = dict(row)
    for key in ('created_at', 'updated_at', 'embedded_at', 'finished_at'):
        if key in values and values[key] is not None and isinstance(values[key], datetime):
            values[key] = values[key].replace(tzinfo=timezone.utc)
    return model.model_validate(values)


def _date(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class MySQLRepositoryBase:
    """共享 asyncmy 连接池、事务与 MySQL GET_LOCK 命名锁的轻量数据库门面。"""

    def __init__(
        self,
        settings: MySQLSettings | None = None,
        *,
        pool: asyncmy.Pool | None = None,
        owns_pool: bool | None = None,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._owns_pool = (pool is None) if owns_pool is None else owns_pool
        self._lock = asyncio.Lock()
        self._closed = False

    async def _initialize(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError('MySQL 数据库仓库已关闭')
            if self._settings is None:
                self._settings = load_mysql_settings()
            if self._pool is None:
                settings = self._settings
                async with asyncio.timeout(settings.operation_timeout_seconds):
                    self._pool = await asyncmy.create_pool(
                        host=settings.host,
                        port=settings.port,
                        user=settings.user,
                        password=settings.password.get_secret_value(),
                        db=settings.database,
                        charset='utf8mb4',
                        autocommit=False,
                        minsize=1,
                        maxsize=10,
                        connect_timeout=settings.connect_timeout_seconds,
                        pool_recycle=300,
                        init_command='SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ',
                    )

    @asynccontextmanager
    async def _transaction(self):
        await self._initialize()
        assert self._pool is not None and self._settings is not None
        async with asyncio.timeout(self._settings.operation_timeout_seconds):
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        yield cursor
                    await connection.commit()
                except BaseException:
                    try:
                        await connection.rollback()
                    except Exception:
                        pass
                    raise

    @asynccontextmanager
    async def user_scope(self, userid: str, *, namespace: str = 'assistant'):
        """独立连接持有 MySQL 命名锁，跨本服务实例串行同一用户；连接丢失立即取消消费。"""
        await self._initialize()
        assert self._settings is not None
        settings = self._settings
        if namespace not in ('assistant', 'robot-inbox', 'robot-user'):
            raise ValueError('锁命名空间不合法')
        name = namespace + ':' + hashlib.sha256((settings.database + '\0' + userid).encode()).hexdigest()[:48]
        connection = await asyncmy.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password.get_secret_value(),
            db=settings.database,
            autocommit=True,
            connect_timeout=settings.connect_timeout_seconds,
        )
        owner = asyncio.current_task()
        monitor = None
        held = False
        try:
            async with asyncio.timeout(settings.operation_timeout_seconds):
                async with connection.cursor() as cursor:
                    await cursor.execute('SELECT GET_LOCK(%s,0)', (name,))
                    held = (await cursor.fetchone())[0] == 1
            if not held:
                yield False
                return

            async def watch():
                try:
                    while True:
                        await asyncio.sleep(1)
                        async with asyncio.timeout(settings.operation_timeout_seconds):
                            await connection.ping(reconnect=False)
                except Exception:
                    if owner is not None:
                        owner.cancel()

            monitor = asyncio.create_task(watch())
            yield True
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            # 关闭专用连接会释放其命名锁，不把锁带回共享连接池。
            try:
                async with asyncio.timeout(settings.operation_timeout_seconds):
                    await connection.ensure_closed()
            except TimeoutError:
                connection.close()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None and self._owns_pool:
            pool.close()
            await pool.wait_closed()
