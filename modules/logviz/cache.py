"""分析缓存。

### 为什么必须有

一份 200 MB 的 access 日志，完整解析一遍要好几秒。用户看完报告点个下钻、
切回来看一眼、再点另一个指标 —— 每次都重扫的话，这个工具根本没法用。

### 缓存的有效性判据：路径 + 大小 + mtime

日志是**最不该被缓存骗到**的东西：它一直在追加。用路径当 key 的缓存，
在日志转了一圈之后会拿着一小时前的报告说"一切正常"，那比没有缓存危险得多。

所以 key 里带上 `size` 和 `mtime`。任何一个变了就是新文件，缓存作废重算。
**宁可多算一次，也不要给出旧结论。**

### 存什么、不存什么

- 存：聚合结果（报告 JSON）+ 文件指纹
- **不存**：日志正文。缓存库不该变成第二份日志副本 ——
  那既没必要，也让"你拷走 db 就带走了一份日志内容"变成新问题

### 边界

- 缓存库在模块自己的 `data/` 下，中台不越权读写
- 容量有上限（`MAX_ENTRIES`），超了按最后访问时间淘汰
- 库里的东西随时可以删 —— 删了只是下次重新分析，不影响用户的任何文件
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from . import context

#: 最多保留多少条分析结果。日志目录换来换去也不会无限涨
MAX_ENTRIES = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis (
    key          TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    kind         TEXT NOT NULL,
    report       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_used_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_analysis_used ON analysis (last_used_at);
"""

_lock = threading.Lock()
_initialised = False


def _connect() -> sqlite3.Connection:
    context.ensure_dirs()
    conn = sqlite3.connect(context.CACHE_DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL 让"一边读报告一边写新报告"不至于互相锁住
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    global _initialised
    if _initialised:
        return
    conn.executescript(_SCHEMA)
    _initialised = True


def make_key(path: Path, size: int, mtime: float, kind: str) -> str:
    """指纹 = 路径 + 大小 + mtime + 类型。

    带上 kind 是因为同一份文件用不同类型解析出来的报告完全不同，
    不能让"我先按 nginx 看过一眼"污染"我改成按 Java 解析"。
    """
    return f"{path}|{size}|{int(mtime * 1000)}|{kind}"


def get(path: Path, size: int, mtime: float, kind: str) -> dict | None:
    key = make_key(path, size, mtime, kind)
    with _lock:
        try:
            with _connect() as conn:
                _ensure(conn)
                row = conn.execute(
                    "SELECT report FROM analysis WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE analysis SET last_used_at = datetime('now','localtime') WHERE key = ?",
                    (key,),
                )
                return json.loads(row["report"])
        except (sqlite3.Error, ValueError):
            # 缓存坏了不是错误 —— 它只是缓存。重新算一遍就是了
            return None


def put(path: Path, size: int, mtime: float, kind: str, report: dict) -> None:
    key = make_key(path, size, mtime, kind)
    payload = json.dumps(report, ensure_ascii=False)
    with _lock:
        try:
            with _connect() as conn:
                _ensure(conn)
                conn.execute(
                    """
                    INSERT INTO analysis (key, path, size, mtime, kind, report)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        report = excluded.report,
                        last_used_at = datetime('now','localtime')
                    """,
                    (key, str(path), size, mtime, kind, payload),
                )
                _evict(conn)
        except sqlite3.Error:
            # 写缓存失败不该让分析失败 —— 报告已经在内存里了，照常返回
            pass


def _evict(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM analysis").fetchone()["n"]
    if count <= MAX_ENTRIES:
        return
    conn.execute(
        """
        DELETE FROM analysis WHERE key IN (
            SELECT key FROM analysis ORDER BY last_used_at ASC LIMIT ?
        )
        """,
        (count - MAX_ENTRIES,),
    )


def stats() -> dict:
    with _lock:
        try:
            with _connect() as conn:
                _ensure(conn)
                row = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM analysis"
                ).fetchone()
                return {
                    "entries": row["n"],
                    "bytes": row["bytes"],
                    "db_path": str(context.CACHE_DB),
                    "max_entries": MAX_ENTRIES,
                }
        except sqlite3.Error as exc:
            return {"entries": 0, "bytes": 0, "error": str(exc)}


def clear() -> int:
    """清空缓存。**动不了用户的任何文件** —— 只是让下次重新分析。"""
    with _lock:
        try:
            with _connect() as conn:
                _ensure(conn)
                row = conn.execute("SELECT COUNT(*) AS n FROM analysis").fetchone()
                conn.execute("DELETE FROM analysis")
                return row["n"]
        except sqlite3.Error:
            return 0


def forget(path: Path) -> None:
    """某个文件变了（或用户要求重算）时，把它的缓存全清掉。"""
    with _lock:
        try:
            with _connect() as conn:
                _ensure(conn)
                conn.execute("DELETE FROM analysis WHERE path = ?", (str(path),))
        except sqlite3.Error:
            pass
