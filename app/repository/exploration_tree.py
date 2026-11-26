"""基于 agent-core 的探索树存储仓库，支持 MySQL 与 SQLite 双后端。

维护两张轻量业务元数据表：
- `exploration_trees(tree_id, owner_userid, name, created_at, updated_at)`
- `exploration_tree_files(tree_id, owner_userid, file_id, filename, created_at)`

并与 `agent-core` 的 `records` / `spaces` / `memory` / `file_notes` 共库协作；
主干 `head`、节点起止耗时与 `display:<task_id>` 展示历史均由 `agent-core` 原生托管。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Literal

from agent_core import MySQLStorage, SQLiteStorage, schema_sql
import asyncmy
from pydantic import AwareDatetime, Field

from app.config.database import MySQLSettings, load_mysql_settings
from app.model.question_tree import QuestionTreeNotFound, TreeID, TreeModel, new_tree_id, utc_now

AGENT_CORE_TREES_OWNER = 'agent-core:trees'

_SQLITE_BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS exploration_trees (
    tree_id TEXT PRIMARY KEY,
    owner_userid TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exploration_trees_owner_updated
    ON exploration_trees (owner_userid, updated_at DESC, tree_id DESC);

CREATE TABLE IF NOT EXISTS exploration_tree_files (
    tree_id TEXT NOT NULL,
    owner_userid TEXT NOT NULL,
    file_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tree_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_exploration_tree_files_owner
    ON exploration_tree_files (owner_userid, tree_id, file_id);
""".strip()

_MYSQL_BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS exploration_trees (
    tree_id VARCHAR(32) NOT NULL PRIMARY KEY,
    owner_userid VARCHAR(128) NOT NULL,
    name VARCHAR(200) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    INDEX idx_exploration_trees_owner_updated (owner_userid, updated_at, tree_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS exploration_tree_files (
    tree_id VARCHAR(32) NOT NULL,
    owner_userid VARCHAR(128) NOT NULL,
    file_id VARCHAR(64) NOT NULL,
    filename VARCHAR(512) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (tree_id, file_id),
    INDEX idx_exploration_tree_files_owner (owner_userid, tree_id, file_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
""".strip()


def agent_core_tree_scope(tree_id: str, *, owner: str = AGENT_CORE_TREES_OWNER) -> str:
    """与 agent-core TaskRepository.scope 完全一致的结构化作用域编码。"""
    return json.dumps([owner, tree_id], ensure_ascii=False, separators=(',', ':'))


def exploration_schema_sql(dialect: Literal['sqlite', 'mysql']) -> str:
    """返回完整的 DDL（agent-core 核心表 + exploration 业务元数据表）。"""
    if dialect == 'sqlite':
        return schema_sql('sqlite').rstrip() + '\n\n' + _SQLITE_BUSINESS_SCHEMA + '\n'
    if dialect == 'mysql':
        return schema_sql('mysql').rstrip() + '\n\n' + _MYSQL_BUSINESS_SCHEMA + '\n'
    raise ValueError(f'不支持的数据库方言: {dialect}')


def init_sqlite_schema(path: str | Path) -> None:
    """在本地 SQLite 文件中初始化 agent-core 与 exploration 全部表结构。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    try:
        conn.executescript(exploration_schema_sql('sqlite'))
        conn.commit()
    finally:
        conn.close()


async def init_mysql_schema(pool: asyncmy.Pool) -> None:
    """在 MySQL 连接池中初始化 agent-core 与 exploration 全部表结构（测试/容器初始化辅助）。"""
    statements = [s.strip() for s in exploration_schema_sql('mysql').split(';') if s.strip()]
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for statement in statements:
                await cur.execute(statement)
        await conn.commit()


def _format_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _normalize_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError('问答树名称不能为空白')
    stripped = name.strip()
    if len(stripped) > 200:
        raise ValueError('问答树名称不能超过 200 个字符')
    return stripped


class ExplorationTreeRecord(TreeModel):
    """探索树业务元数据投影。"""

    tree_id: TreeID = Field(description='问答树 ID。')
    owner_userid: str = Field(strict=True, min_length=1, max_length=128)
    name: str = Field(strict=True, min_length=1, max_length=200)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @property
    def id(self) -> str:
        return self.tree_id


class ExplorationTreeRepository:
    """探索树业务元数据与 agent-core 存储协调仓库。

    同时支持 SQLite（用于单元测试与本地隔离运行）和 MySQL（用于生产部署）。
    """

    def __init__(
        self,
        *,
        dialect: Literal['sqlite', 'mysql'],
        sqlite_path: Path | None = None,
        mysql_settings: MySQLSettings | None = None,
        mysql_pool: asyncmy.Pool | None = None,
        owns_pool: bool = True,
        initialize_sqlite: bool = False,
        owner_namespace: str = AGENT_CORE_TREES_OWNER,
    ) -> None:
        self._dialect = dialect
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._mysql_settings = mysql_settings
        self._pool = mysql_pool
        self._owns_pool = owns_pool
        self._initialize_sqlite = initialize_sqlite
        self._owner_namespace = owner_namespace
        self._storage: SQLiteStorage | MySQLStorage | None = None
        self._sqlite_conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_sqlite(
        cls,
        path: str | Path,
        *,
        initialize: bool = True,
    ) -> ExplorationTreeRepository:
        return cls(dialect='sqlite', sqlite_path=Path(path), initialize_sqlite=initialize)

    @classmethod
    def from_mysql(
        cls,
        settings: MySQLSettings | None = None,
        *,
        pool: asyncmy.Pool | None = None,
    ) -> ExplorationTreeRepository:
        return cls(
            dialect='mysql',
            mysql_settings=settings,
            mysql_pool=pool,
            owns_pool=pool is None,
        )

    @property
    def dialect(self) -> Literal['sqlite', 'mysql']:
        return self._dialect

    async def ensure_connected(self) -> SQLiteStorage | MySQLStorage:
        """延迟建立存储连接并返回供 AgentRuntime 使用的 Storage 适配器。"""
        if self._closed:
            raise RuntimeError('探索树存储仓库已关闭')
        if self._storage is not None:
            return self._storage
        async with self._lock:
            if self._closed:
                raise RuntimeError('探索树存储仓库已关闭')
            if self._storage is not None:
                return self._storage
            if self._dialect == 'sqlite':
                if self._sqlite_path is None:
                    raise ValueError('SQLite 路径不能为空')
                if self._initialize_sqlite:
                    await asyncio.to_thread(init_sqlite_schema, self._sqlite_path)
                self._storage = SQLiteStorage.connect(self._sqlite_path)
                self._sqlite_conn = sqlite3.connect(str(self._sqlite_path), check_same_thread=False)
                self._sqlite_conn.row_factory = sqlite3.Row
                return self._storage
            if self._pool is None:
                settings = self._mysql_settings or load_mysql_settings()
                self._pool = await asyncmy.create_pool(
                    host=settings.host,
                    port=settings.port,
                    user=settings.user,
                    password=settings.password.get_secret_value(),
                    db=settings.database,
                    charset='utf8mb4',
                    autocommit=False,
                    connect_timeout=settings.connect_timeout_seconds,
                    minsize=1,
                    maxsize=10,
                )
            self._storage = await MySQLStorage.connect(self._pool)
            return self._storage

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            storage, self._storage = self._storage, None
            if storage is not None:
                await storage.close()
            if self._sqlite_conn is not None:
                conn, self._sqlite_conn = self._sqlite_conn, None
                await asyncio.to_thread(conn.close)
            if self._pool is not None and self._owns_pool:
                pool, self._pool = self._pool, None
                pool.close()
                await pool.wait_closed()

    async def _run_sqlite(self, fn):
        await self.ensure_connected()
        assert self._sqlite_conn is not None
        async with self._lock:
            return await asyncio.to_thread(fn, self._sqlite_conn)

    async def create_tree(
        self,
        owner_userid: str,
        name: str,
        *,
        tree_id: str | None = None,
    ) -> ExplorationTreeRecord:
        normalized = _normalize_name(name)
        tid = tree_id or new_tree_id()
        now = utc_now()
        now_str = _format_utc(now)
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection) -> None:
                conn.execute(
                    'INSERT INTO exploration_trees (tree_id, owner_userid, name, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (tid, owner_userid, normalized, now_str, now_str),
                )
                conn.commit()
            await self._run_sqlite(_op)
        else:
            await self.ensure_connected()
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        'INSERT INTO exploration_trees (tree_id, owner_userid, name, created_at, updated_at) '
                        'VALUES (%s, %s, %s, %s, %s)',
                        (tid, owner_userid, normalized, now_str, now_str),
                    )
                await conn.commit()
        return ExplorationTreeRecord(
            tree_id=tid,
            owner_userid=owner_userid,
            name=normalized,
            created_at=now,
            updated_at=now,
        )

    async def list_trees(self, owner_userid: str) -> list[ExplorationTreeRecord]:
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection) -> list[tuple]:
                cur = conn.execute(
                    'SELECT tree_id, owner_userid, name, created_at, updated_at '
                    'FROM exploration_trees WHERE owner_userid = ? '
                    'ORDER BY updated_at DESC, tree_id DESC',
                    (owner_userid,),
                )
                return cur.fetchall()
            rows = await self._run_sqlite(_op)
        else:
            await self.ensure_connected()
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        'SELECT tree_id, owner_userid, name, created_at, updated_at '
                        'FROM exploration_trees WHERE owner_userid = %s '
                        'ORDER BY updated_at DESC, tree_id DESC',
                        (owner_userid,),
                    )
                    rows = await cur.fetchall()
        return [
            ExplorationTreeRecord(
                tree_id=row[0],
                owner_userid=row[1],
                name=row[2],
                created_at=_parse_utc(row[3]),
                updated_at=_parse_utc(row[4]),
            )
            for row in rows
        ]

    async def get_tree(self, tree_id: str, owner_userid: str) -> ExplorationTreeRecord:
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection):
                cur = conn.execute(
                    'SELECT tree_id, owner_userid, name, created_at, updated_at '
                    'FROM exploration_trees WHERE tree_id = ? AND owner_userid = ?',
                    (tree_id, owner_userid),
                )
                return cur.fetchone()
            row = await self._run_sqlite(_op)
        else:
            await self.ensure_connected()
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        'SELECT tree_id, owner_userid, name, created_at, updated_at '
                        'FROM exploration_trees WHERE tree_id = %s AND owner_userid = %s',
                        (tree_id, owner_userid),
                    )
                    row = await cur.fetchone()
        if row is None:
            raise QuestionTreeNotFound('问答树不存在')
        return ExplorationTreeRecord(
            tree_id=row[0],
            owner_userid=row[1],
            name=row[2],
            created_at=_parse_utc(row[3]),
            updated_at=_parse_utc(row[4]),
        )

    async def rename_tree(self, tree_id: str, owner_userid: str, name: str) -> ExplorationTreeRecord:
        normalized = _normalize_name(name)
        now = utc_now()
        now_str = _format_utc(now)
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection):
                cur = conn.execute(
                    'UPDATE exploration_trees SET name = ?, updated_at = ? '
                    'WHERE tree_id = ? AND owner_userid = ?',
                    (normalized, now_str, tree_id, owner_userid),
                )
                conn.commit()
                if cur.rowcount == 0:
                    return None
                row = conn.execute(
                    'SELECT tree_id, owner_userid, name, created_at, updated_at '
                    'FROM exploration_trees WHERE tree_id = ? AND owner_userid = ?',
                    (tree_id, owner_userid),
                ).fetchone()
                return row
            row = await self._run_sqlite(_op)
        else:
            await self.ensure_connected()
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        'UPDATE exploration_trees SET name = %s, updated_at = %s '
                        'WHERE tree_id = %s AND owner_userid = %s',
                        (normalized, now_str, tree_id, owner_userid),
                    )
                    if cur.rowcount == 0:
                        await conn.rollback()
                        raise QuestionTreeNotFound('问答树不存在')
                    await cur.execute(
                        'SELECT tree_id, owner_userid, name, created_at, updated_at '
                        'FROM exploration_trees WHERE tree_id = %s AND owner_userid = %s',
                        (tree_id, owner_userid),
                    )
                    row = await cur.fetchone()
                await conn.commit()
        if row is None:
            raise QuestionTreeNotFound('问答树不存在')
        return ExplorationTreeRecord(
            tree_id=row[0],
            owner_userid=row[1],
            name=row[2],
            created_at=_parse_utc(row[3]),
            updated_at=_parse_utc(row[4]),
        )

    async def touch_tree(
        self,
        tree_id: str,
        owner_userid: str,
        *,
        updated_at: datetime | None = None,
    ) -> datetime:
        now = updated_at or utc_now()
        now_str = _format_utc(now)
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection) -> int:
                cur = conn.execute(
                    'UPDATE exploration_trees SET updated_at = ? '
                    'WHERE tree_id = ? AND owner_userid = ?',
                    (now_str, tree_id, owner_userid),
                )
                conn.commit()
                return cur.rowcount
            affected = await self._run_sqlite(_op)
        else:
            await self.ensure_connected()
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        'UPDATE exploration_trees SET updated_at = %s '
                        'WHERE tree_id = %s AND owner_userid = %s',
                        (now_str, tree_id, owner_userid),
                    )
                    affected = cur.rowcount
                await conn.commit()
        if affected == 0:
            raise QuestionTreeNotFound('问答树不存在')
        return now

    async def register_files(
        self,
        tree_id: str,
        owner_userid: str,
        files: Sequence[tuple[str, str]],
    ) -> None:
        if not files:
            return
        now_str = _format_utc(utc_now())
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection) -> None:
                for file_id, filename in files:
                    conn.execute(
                        'INSERT OR IGNORE INTO exploration_tree_files '
                        '(tree_id, owner_userid, file_id, filename, created_at) '
                        'VALUES (?, ?, ?, ?, ?)',
                        (tree_id, owner_userid, file_id, filename, now_str),
                    )
                conn.commit()
            await self._run_sqlite(_op)
        else:
            await self.ensure_connected()
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    for file_id, filename in files:
                        await cur.execute(
                            'INSERT IGNORE INTO exploration_tree_files '
                            '(tree_id, owner_userid, file_id, filename, created_at) '
                            'VALUES (%s, %s, %s, %s, %s)',
                            (tree_id, owner_userid, file_id, filename, now_str),
                        )
                await conn.commit()

    async def has_file(self, tree_id: str, owner_userid: str, file_id: str) -> bool:
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection) -> bool:
                row = conn.execute(
                    'SELECT 1 FROM exploration_tree_files '
                    'WHERE tree_id = ? AND owner_userid = ? AND file_id = ?',
                    (tree_id, owner_userid, file_id),
                ).fetchone()
                return row is not None
            return await self._run_sqlite(_op)
        await self.ensure_connected()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    'SELECT 1 FROM exploration_tree_files '
                    'WHERE tree_id = %s AND owner_userid = %s AND file_id = %s',
                    (tree_id, owner_userid, file_id),
                )
                return (await cur.fetchone()) is not None

    async def get_file_name(self, tree_id: str, owner_userid: str, file_id: str) -> str | None:
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection) -> str | None:
                row = conn.execute(
                    'SELECT filename FROM exploration_tree_files '
                    'WHERE tree_id = ? AND owner_userid = ? AND file_id = ?',
                    (tree_id, owner_userid, file_id),
                ).fetchone()
                return str(row[0]) if row is not None else None
            return await self._run_sqlite(_op)
        await self.ensure_connected()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    'SELECT filename FROM exploration_tree_files '
                    'WHERE tree_id = %s AND owner_userid = %s AND file_id = %s',
                    (tree_id, owner_userid, file_id),
                )
                row = await cur.fetchone()
                return str(row[0]) if row is not None else None

    async def delete_tree_data(self, tree_id: str, owner_userid: str) -> list[str]:
        """原子清理 agent-core 存储行（含 display:<task_id>）与业务元数据表，返回待删磁盘的 file_id 列表。"""
        scope = agent_core_tree_scope(tree_id, owner=self._owner_namespace)
        if self._dialect == 'sqlite':
            def _op(conn: sqlite3.Connection) -> list[str]:
                exists = conn.execute(
                    'SELECT 1 FROM exploration_trees WHERE tree_id = ? AND owner_userid = ?',
                    (tree_id, owner_userid),
                ).fetchone()
                if exists is None:
                    raise QuestionTreeNotFound('问答树不存在')
                file_rows = conn.execute(
                    'SELECT file_id FROM exploration_tree_files '
                    'WHERE tree_id = ? AND owner_userid = ?',
                    (tree_id, owner_userid),
                ).fetchall()
                file_ids = [str(row[0]) for row in file_rows]
                # 同步清理 SQLite FTS 虚拟表对应的 rowid，避免孤儿索引。
                conn.execute(
                    'DELETE FROM memory_fts WHERE rowid IN (SELECT rowid FROM memory WHERE scope = ?)',
                    (scope,),
                )
                conn.execute(
                    'DELETE FROM file_notes_fts WHERE rowid IN (SELECT rowid FROM file_notes WHERE scope = ?)',
                    (scope,),
                )
                for table in ('records', 'spaces', 'memory', 'file_notes'):
                    conn.execute(f'DELETE FROM {table} WHERE scope = ?', (scope,))
                conn.execute(
                    'DELETE FROM exploration_tree_files WHERE tree_id = ? AND owner_userid = ?',
                    (tree_id, owner_userid),
                )
                conn.execute(
                    'DELETE FROM exploration_trees WHERE tree_id = ? AND owner_userid = ?',
                    (tree_id, owner_userid),
                )
                conn.commit()
                return file_ids
            return await self._run_sqlite(_op)

        await self.ensure_connected()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    'SELECT 1 FROM exploration_trees WHERE tree_id = %s AND owner_userid = %s FOR UPDATE',
                    (tree_id, owner_userid),
                )
                if await cur.fetchone() is None:
                    await conn.rollback()
                    raise QuestionTreeNotFound('问答树不存在')
                await cur.execute(
                    'SELECT file_id FROM exploration_tree_files '
                    'WHERE tree_id = %s AND owner_userid = %s',
                    (tree_id, owner_userid),
                )
                file_ids = [str(row[0]) for row in await cur.fetchall()]
                for table in ('records', 'spaces', 'memory', 'file_notes'):
                    await cur.execute(f'DELETE FROM {table} WHERE scope = %s', (scope,))
                await cur.execute(
                    'DELETE FROM exploration_tree_files WHERE tree_id = %s AND owner_userid = %s',
                    (tree_id, owner_userid),
                )
                await cur.execute(
                    'DELETE FROM exploration_trees WHERE tree_id = %s AND owner_userid = %s',
                    (tree_id, owner_userid),
                )
            await conn.commit()
            return file_ids
