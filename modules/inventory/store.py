"""本模块的数据访问层：`resource` / `task` / `event` 三张表的读写。

从 `backend/app/repositories/resource_repo.py` 迁来。**表结构与数据一字未改** ——
表由中台的迁移文件定义，模块只是它的写者之一（概览页也在读它）。

两条核心约束，都来自实际教训：

1. **重扫必须幂等**。同一来源（source）+ 同一来源键（source_key）视为同一条记录，
   重复扫描只更新、不新增，否则扫三次就出现三份重复清单。

2. **扫描不得覆盖用户的决定**。别名、标签、分组、收藏、忽略、是否纳管
   都是用户手工设置的，重扫时一个字都不能动 —— 否则用户整理好的东西
   会被下一轮扫描抹平。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from . import context

# 扫描可以更新的列（纯客观元数据）
_SCAN_UPDATE_COLUMNS = (
    "name",
    "path_or_url",
    "version",
    "publisher",
    "install_date",
    "install_location",
    "uninstall_string",
    "size_bytes",
    "size_source",
)

_SORTABLE = {
    "name": "name COLLATE NOCASE ASC",
    "size": "size_bytes IS NULL, size_bytes DESC",
    "installed": "install_date IS NULL, install_date DESC",
    "recent": "last_used_at IS NULL, last_used_at DESC",
    "used": "use_count DESC, name COLLATE NOCASE ASC",
}


def _connect() -> sqlite3.Connection:
    context.ensure_dirs()
    # 参数必须与中台的连接保持一致：同一个库被两个连接写，
    # journal_mode / busy_timeout 不一致会变成偶发 "database is locked"。
    conn = sqlite3.connect(str(context.DB_PATH), timeout=15.0, isolation_level=None)
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


def upsert_from_scan(
    conn: sqlite3.Connection, source: str, items: list[dict], *, res_type: str = "app"
) -> dict:
    """按 (source, source_key) 幂等写入扫描结果。"""
    existing: dict[str, sqlite3.Row] = {
        row["source_key"]: row
        for row in conn.execute(
            "SELECT id, source_key FROM resource WHERE source = ? AND source_key IS NOT NULL",
            (source,),
        )
    }

    inserted = 0
    updated = 0

    for item in items:
        key = item.get("source_key")
        if not key:
            continue

        if key in existing:
            assignments = ", ".join(f"{col} = ?" for col in _SCAN_UPDATE_COLUMNS)
            params = [item.get(col) for col in _SCAN_UPDATE_COLUMNS]
            params.append(existing[key]["id"])
            conn.execute(
                f"UPDATE resource SET {assignments}, "
                f"updated_at = datetime('now','localtime') WHERE id = ?",
                params,
            )
            updated += 1
        else:
            columns = ["type", "source", "source_key", *_SCAN_UPDATE_COLUMNS]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO resource ({', '.join(columns)}) VALUES ({placeholders})",
                [res_type, source, key, *[item.get(col) for col in _SCAN_UPDATE_COLUMNS]],
            )
            inserted += 1

    return {"inserted": inserted, "updated": updated, "total": inserted + updated}


def list_resources(
    conn: sqlite3.Connection,
    *,
    res_type: str | None = None,
    source: str | None = None,
    query: str | None = None,
    include_ignored: bool = False,
    sort: str = "name",
    limit: int = 2000,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[object] = []

    if res_type:
        where.append("type = ?")
        params.append(res_type)
    if source:
        where.append("source = ?")
        params.append(source)
    if not include_ignored:
        where.append("ignored = 0")
    if query:
        pattern = f"%{query}%"
        where.append(
            "(name LIKE ? OR IFNULL(alias,'') LIKE ? OR IFNULL(publisher,'') LIKE ? "
            "OR IFNULL(path_or_url,'') LIKE ?)"
        )
        params.extend([pattern] * 4)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = _SORTABLE.get(sort, _SORTABLE["name"])
    params.extend([limit, offset])

    rows = conn.execute(
        f"SELECT * FROM resource {clause} ORDER BY {order} LIMIT ? OFFSET ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def get_resource(conn: sqlite3.Connection, resource_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM resource WHERE id = ?", (resource_id,)).fetchone()
    return dict(row) if row else None


_USER_FIELDS = {"alias", "tags", "group_name", "favorite", "ignored", "managed", "note", "enabled"}


def update_user_fields(conn: sqlite3.Connection, resource_id: int, changes: dict) -> dict | None:
    """只允许改用户字段，避免界面误传把扫描元数据写坏。"""
    payload = {k: v for k, v in changes.items() if k in _USER_FIELDS}
    if not payload:
        return get_resource(conn, resource_id)
    assignments = ", ".join(f"{k} = ?" for k in payload)
    conn.execute(
        f"UPDATE resource SET {assignments}, updated_at = datetime('now','localtime') "
        f"WHERE id = ?",
        [*payload.values(), resource_id],
    )
    return get_resource(conn, resource_id)


def mark_used(conn: sqlite3.Connection, resource_id: int) -> None:
    conn.execute(
        "UPDATE resource SET use_count = use_count + 1, "
        "last_used_at = datetime('now','localtime'), "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (resource_id,),
    )


def record_task(
    conn: sqlite3.Connection,
    *,
    kind: str,
    machine_id: str,
    status: str,
    resource_id: int | None = None,
    item_name: str | None = None,
    params: dict | None = None,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    output_path: str | None = None,
    message: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO task (kind, resource_id, item_name, params, status, exit_code,
                          started_at, finished_at, duration_ms, machine_id,
                          output_path, message)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'),
                datetime('now','localtime'), ?, ?, ?, ?)
        """,
        (
            kind,
            resource_id,
            item_name,
            json.dumps(params, ensure_ascii=False) if params else None,
            status,
            exit_code,
            duration_ms,
            machine_id,
            output_path,
            message,
        ),
    )
    return int(cursor.lastrowid or 0)


def record_event(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: str = "web",
    target_type: str | None = None,
    target_id: int | None = None,
    target_name: str | None = None,
    detail: dict | None = None,
    ip: str = "",
) -> None:
    """写一条审计流水。

    中台自己的代码里也有一份等价实现（`repositories/resource_repo.py`）——
    这是刻意的重复：`event` 表由中台定义、被多方写入，但**模块不 import 中台
    的包**（那样就绑死了）。两份写入的列完全一致，读的人不在意是谁写的。
    """
    conn.execute(
        """
        INSERT INTO event (actor, action, target_type, target_id, target_name, detail, ip)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor,
            action,
            target_type,
            target_id,
            target_name,
            json.dumps(detail, ensure_ascii=False) if detail else None,
            ip,
        ),
    )
