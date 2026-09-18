"""SQLite 接入与版本化迁移。

不使用 ORM：本项目的表结构会随认知变化频繁调整，
手写 SQL + 显式迁移文件比 autogenerate 更可控，也更容易在别人机器上复现。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import config

_MIGRATION_TABLE = "_migrations"


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    # autocommit 模式：写操作立即落盘，省掉「忘记 commit」这类低级故障。
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def run_migrations() -> list[str]:
    """按文件名顺序执行未应用过的迁移，返回本次实际应用的迁移名。"""
    files = sorted(config.MIGRATIONS_DIR.glob("*.sql"))
    applied: list[str] = []
    with get_conn() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} (
                name       TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        done = {row["name"] for row in conn.execute(f"SELECT name FROM {_MIGRATION_TABLE}")}
        for path in files:
            if path.name in done:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(f"INSERT INTO {_MIGRATION_TABLE}(name) VALUES (?)", (path.name,))
            applied.append(path.name)
    return applied


def checkpoint() -> str:
    """把 WAL 合并回主库，让备份只需拷一个文件。

    强制杀进程时 WAL 不会被合并，会留下一个可能比主库还大的 -wal。
    所以正常收工时应主动做一次。
    """
    with get_conn() as conn:
        health = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA optimize")
    return health
