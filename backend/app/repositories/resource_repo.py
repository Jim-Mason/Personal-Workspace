"""平台侧对 `resource` / `task` / `event` 三张表的**读取**，以及审计流水写入。

### 这个文件在模块一迁出后变小了，这是对的

三张表里：

- `event`（审计流水）与 `task`（任务历史）是**平台级**的 —— 品牌改动、模块启停
  都往里写，概览页与「审计流水」标签页读它
- `resource` 的行由**模块一**（`modules/inventory/`）负责写

所以 `resource_repo.py` 原先承担的「resource 表读写」整体搬进了
`modules/inventory/store.py`，这里只留下平台自己还要用的三件事：
写审计流水、读最近流水、拼概览。

### 为什么两边各有一份 `record_event`

`event` 表由中台的表结构定义、被两方写入，但**模块不 import 中台的包** ——
那是「模块可脱离中台存在」的前提。所以模块里有一份等价的插入实现
（`modules/inventory/store.py` 的 `record_event`），列完全一致。
读的人（概览、审计页）不关心是谁写的。

这是刻意接受的少量重复，换来的是模块与平台零耦合。
"""

from __future__ import annotations

import json
import sqlite3


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


def recent_events(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def overview(conn: sqlite3.Connection) -> dict:
    """概览页的数据。

    注意它读的是 `resource` 表 —— 那张表的**写**归模块一，读归概览页。
    模块被停用时这里会如实反映「一条都没有」，而不是报错 ——
    概览页不该因为一个模块没开就整页崩掉。
    """
    app_count = conn.execute(
        "SELECT COUNT(*) FROM resource WHERE type = 'app' AND ignored = 0"
    ).fetchone()[0]
    known_size = conn.execute(
        "SELECT IFNULL(SUM(size_bytes), 0) FROM resource "
        "WHERE type = 'app' AND ignored = 0 AND size_bytes IS NOT NULL"
    ).fetchone()[0]
    unknown_size = conn.execute(
        "SELECT COUNT(*) FROM resource WHERE type = 'app' AND ignored = 0 "
        "AND size_bytes IS NULL"
    ).fetchone()[0]
    ignored_count = conn.execute("SELECT COUNT(*) FROM resource WHERE ignored = 1").fetchone()[0]
    by_source = [
        dict(row)
        for row in conn.execute(
            "SELECT source, COUNT(*) AS count FROM resource WHERE type = 'app' "
            "AND ignored = 0 GROUP BY source ORDER BY count DESC"
        )
    ]
    recent_installed = [
        dict(row)
        for row in conn.execute(
            "SELECT id, name, publisher, version, install_date, size_bytes FROM resource "
            "WHERE type = 'app' AND ignored = 0 AND install_date IS NOT NULL "
            "ORDER BY install_date DESC LIMIT 8"
        )
    ]
    largest = [
        dict(row)
        for row in conn.execute(
            "SELECT id, name, publisher, size_bytes FROM resource "
            "WHERE type = 'app' AND ignored = 0 AND size_bytes IS NOT NULL "
            "ORDER BY size_bytes DESC LIMIT 8"
        )
    ]
    task_count = conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]
    last_scan = conn.execute(
        "SELECT * FROM task WHERE kind = 'scan' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "app_count": app_count,
        "known_size_bytes": known_size,
        "unknown_size_count": unknown_size,
        "ignored_count": ignored_count,
        "by_source": by_source,
        "recent_installed": recent_installed,
        "largest": largest,
        "task_count": task_count,
        "last_scan": dict(last_scan) if last_scan else None,
    }
