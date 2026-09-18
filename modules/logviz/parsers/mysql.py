"""MySQL 慢查询日志（slow query log）。

### 为什么它的价值密度最高

nginx 告诉你"这个接口慢了"，Java 日志告诉你"这里抛错了"，
**慢查询告诉你"到底是哪条 SQL 把库拖住了"**。前面两份是症状，这份是病因。

### 认得什么

MySQL 慢查询是**块结构**，不是行结构 —— 这是它和另外两种日志的根本区别：

    # Time: 2026-09-18T12:33:39.123456+08:00
    # User@Host: app[app] @  [172.16.0.9]  Id: 88213
    # Query_time: 12.480921  Lock_time: 0.000213 Rows_sent: 3  Rows_examined: 4820113
    SET timestamp=1758166419;
    SELECT * FROM device_properties_message WHERE device_id = 88213 ORDER BY ts DESC LIMIT 3;

所以解析必须先**按 `# ` 开头的行切块**，再在块内取字段。
漏掉这一点的话，`# Query_time:` 那些行会被当成 SQL 正文，统计出来全是废话。

### 报告里有什么

| 指标 | 回答什么问题 |
|---|---|
| 最慢 SQL Top N | 哪几条 SQL 最费时间（**归一化后**，同一模板只占一行） |
| 扫描行数 Top N | 哪条 SQL 在扫全表（`Rows_examined` 特别大 = 多半没走索引） |
| 锁等待 | `Lock_time` 排前几的语句 |
| 按库 / 按用户 | 哪个库、哪个账号在制造慢查询 |
| 返回行数 vs 扫描行数 | 扫描远大于返回 = 索引没吃上，这是最值钱的那条线索 |
| 时间直方图 | 慢查询在什么时候集中出现 |

### 归一化的分寸

SQL 的归一化比日志消息更需要小心：把 `WHERE id = 1` 和 `WHERE id = 2`
归成一条是对的，但**不能把 `WHERE` 和 `ORDER BY` 之间的一切都抹掉** ——
那样两条结构完全不同的语句会被误并。

这里的做法是只替换**字面量**（数字、字符串、IN 列表），
关键词与标识符一律保留。宁可少并几条，也不要并错。
"""

from __future__ import annotations

import re
from collections import Counter

from .. import common

KIND = "mysql"
LABEL = "MySQL 慢查询"

_BLOCK_HEAD_RE = re.compile(r"^#\s*(?P<key>[A-Za-z_@# ]+?)\s*:\s*(?P<value>.*)$")

#: `# User@Host: app[app] @  [172.16.0.9]  Id: 88213`
_USER_HOST_RE = re.compile(
    r"^(?P<user>[\w.\-]*)\[(?P<user2>[^\]]*)\]\s*@\s*(?P<host>[\w.\-]*)\s*"
    r"\[(?P<ip>[^\]]*)\]\s*Id:\s*(?P<id>\d+)"
)

#: `# Query_time: 12.480921  Lock_time: 0.000213 Rows_sent: 3  Rows_examined: 4820113`
#:
#: ⚠️ 开头那个 `Query_time:` 必须写成**可选**。
#: 因为 `_BLOCK_HEAD_RE` 已经把它当成 key 吃掉了（value 从数字开始），
#: 而 MySQL 在某些版本/配置下也会把它单独成行。两种形态都要能认。
#: 只写必需的话，value 是 `12.480921  Lock_time: ...` 就永远匹配不上 ——
#: 表现是"块切对了，Query_time 全是 0"，正是这个 bug。
_METRICS_RE = re.compile(
    r"(?:Query_time:\s*)?(?P<query_time>\d+(?:\.\d+)?)\s+"
    r"(?:Lock_time:\s*)?(?P<lock_time>\d+(?:\.\d+)?)\s+"
    r"(?:Rows_sent:\s*)?(?P<rows_sent>\d+)\s+"
    r"(?:Rows_examined:\s*)?(?P<rows_examined>\d+)"
)

_SET_TIMESTAMP_RE = re.compile(r"^SET\s+timestamp\s*=", re.I)
_USE_DB_RE = re.compile(r"^use\s+(?P<db>[\w$]+)\s*;?\s*$", re.I)

#: SQL 字面量归一化
_STR_LIT_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_NUM_LIT_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEX_LIT_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_IN_LIST_RE = re.compile(r"\bIN\s*\([^)]*\)", re.I)
_WS_RE = re.compile(r"\s+")

#: 有这些关键词才当成 SQL 正文（防止把注释行当 SQL）
_SQL_START_RE = re.compile(
    r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|MERGE|CALL|CREATE|ALTER|DROP|TRUNCATE|"
    r"OPTIMIZE|ANALYZE|RENAME|LOAD|WITH|BEGIN|COMMIT|ROLLBACK)\b",
    re.I,
)


def sniff(rows: list[tuple[int, str]], sample: int = 200) -> float:
    """有多少把握是 MySQL 慢查询日志。返回 0~1。"""
    blocks = 0
    metrics = 0
    checked = 0
    for _, text in rows[:sample]:
        if not text.strip():
            continue
        checked += 1
        if _BLOCK_HEAD_RE.match(text) and "Time" in text[:12]:
            blocks += 1
        if "Query_time:" in text and "Rows_examined:" in text:
            metrics += 1
    if not checked:
        return 0.0
    # `# Query_time: ... Rows_examined:` 这一行是慢查询日志独有的指纹，
    # 别的日志里基本不会同时出现这两个词
    if metrics:
        return 1.0
    return min(1.0, blocks / checked * 2)


class SlowQuery:
    __slots__ = (
        "lineno", "end_line", "moment", "user", "host", "db", "query_time",
        "lock_time", "rows_sent", "rows_examined", "sql",
    )

    def __init__(self) -> None:
        self.lineno = 0
        self.end_line = 0
        self.moment = None
        self.user = ""
        self.host = ""
        self.db = ""
        self.query_time = 0.0
        self.lock_time = 0.0
        self.rows_sent = 0
        self.rows_examined = 0
        self.sql = ""


def normalize_sql(sql: str) -> str:
    """把 SQL 归成一个"模板"：只抹字面量，保留结构。

    分两步：先把字符串字面量和 IN 列表换掉（它们最容易被误伤），
    再换裸数字。顺序反了的话，一个含数字的字符串会被拆得面目全非。
    """
    out = _STR_LIT_RE.sub("?", sql)
    out = _IN_LIST_RE.sub("IN (?)", out)
    out = _HEX_LIT_RE.sub("?", out)
    out = _NUM_LIT_RE.sub("?", out)
    out = _WS_RE.sub(" ", out).strip()
    return out


def split_queries(rows: list[tuple[int, str]]) -> list[SlowQuery]:
    """按 `# Time:` 切块，解析出每一条慢查询。

    切块的依据是 `# Time:` 行 —— 它是每条慢查询的固定开头。
    块内的 `# User@Host:` / `# Query_time:` / `SET timestamp=` 都是元数据，
    SQL 正文从第一条像 SQL 的行开始。
    """
    queries: list[SlowQuery] = []
    current: SlowQuery | None = None
    sql_parts: list[str] = []
    sql_started = False
    last_db = ""

    def flush() -> None:
        nonlocal current, sql_parts, sql_started
        if current is not None:
            current.sql = " ".join(sql_parts).strip()
            queries.append(current)
        sql_parts = []
        sql_started = False

    for lineno, text in rows:
        stripped = text.strip()
        head = _BLOCK_HEAD_RE.match(text)

        if head and head.group("key").strip().lower() == "time":
            flush()
            current = SlowQuery()
            current.lineno = lineno
            current.moment = _parse_time_value(head.group("value"))
            continue

        if current is None:
            # 块还没开始（文件开头的杂项），跳过
            continue

        current.end_line = lineno

        if head:
            # `.strip()` 不能省：正则里的 `[\w@#$ ]+?` 会因为懒匹配把 `#` 后面那个
            # 空格一起吞进 key（实测得到 `' Time'`），于是下面所有 `key == "time"`
            # 的比较全部落空 —— 表现是"块切对了，但 Query_time 全是 0"。
            key = head.group("key").strip().lower()
            value = head.group("value")
            if key == "user@host":
                parsed = _USER_HOST_RE.match(value.strip())
                if parsed:
                    current.user = parsed.group("user2") or parsed.group("user")
                    current.host = parsed.group("ip") or parsed.group("host")
                    current.db = last_db
            elif key == "query_time":
                metrics = _METRICS_RE.search(value)
                if metrics:
                    current.query_time = float(metrics.group("query_time"))
                    current.lock_time = float(metrics.group("lock_time"))
                    current.rows_sent = int(metrics.group("rows_sent"))
                    current.rows_examined = int(metrics.group("rows_examined"))
            elif key == "schema":
                current.db = value.strip() or current.db
            continue

        if _SET_TIMESTAMP_RE.match(stripped):
            if not current.moment:
                current.moment = _parse_set_timestamp(stripped)
            if not sql_started:
                continue

        use = _USE_DB_RE.match(stripped)
        if use and not sql_started:
            last_db = use.group("db")
            current.db = last_db
            continue

        if not sql_started:
            if _SQL_START_RE.match(stripped):
                sql_started = True
                sql_parts.append(stripped)
            continue

        if _SQL_START_RE.match(stripped) or not stripped:
            # 下一条 SQL（同一条慢查询里偶尔会连着两句）
            sql_parts.append(stripped)
        else:
            sql_parts.append(stripped)

    flush()
    return queries


def _parse_time_value(value: str):
    """`# Time:` 的值：ISO 带微秒与时区，也可能是不带时区的老格式。"""
    raw = value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            from datetime import datetime

            return datetime.strptime(raw.replace(" ", "T") if "T" not in raw and fmt.startswith("%Y-%m-%dT") else raw, fmt)
        except ValueError:
            continue
    return None


def _parse_set_timestamp(text: str):
    """`SET timestamp=1758166419;` -> datetime（兜底：`# Time:` 缺失时用）。"""
    m = re.search(r"timestamp\s*=\s*(\d+)", text)
    if not m:
        return None
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(m.group(1)))
    except (ValueError, OSError, OverflowError):
        return None


class Report:
    def __init__(self) -> None:
        self.total = 0
        self.hist = common.Histogram()
        self.slowest = common.TopN(25)
        self.scanners = common.TopN(25)
        self.locks = common.TopN(15)
        self.databases = common.TopN(15)
        self.users = common.TopN(15)
        self.query_time_total = 0.0
        self.rows_examined_total = 0
        self.rows_sent_total = 0
        self.worst_ratio = common.TopN(15)
        self.anchor: dict[str, int] = {}

    def to_dict(self) -> dict:
        return {
            "kind": KIND,
            "label": LABEL,
            "totals": {
                "slow_queries": self.total,
                "query_time_total": round(self.query_time_total, 3),
                "rows_examined_total": self.rows_examined_total,
                "rows_sent_total": self.rows_sent_total,
                "avg_query_time": round(self.query_time_total / self.total, 4) if self.total else 0,
            },
            "timeline": self.hist.to_dict(),
            # 这几个榜按**度量**排，不按出现次数 —— 见 common.TopN.to_ranked
            "slowest": self.slowest.to_ranked(key_name="sql"),
            "full_scans": self.scanners.to_ranked(key_name="sql"),
            "locks": self.locks.to_ranked(key_name="sql"),
            "worst_ratio": self.worst_ratio.to_ranked(key_name="sql"),
            # 这两个按次数排（"谁制造得最多"就是要按次数）
            "databases": self.databases.to_list(key_name="db"),
            "users": self.users.to_list(key_name="user"),
            "notes": self._notes(),
        }

    def _notes(self) -> list[str]:
        notes = []
        if not self.total:
            notes.append("这份文件里没有解析出任何一条慢查询。确认它是 MySQL 的 slow query log。")
        else:
            ratio = self.rows_examined_total / max(1, self.rows_sent_total)
            if ratio > 100:
                notes.append(
                    f"整体「扫描行数 / 返回行数」= {ratio:,.0f} 倍 —— "
                    "说明有相当一部分查询在扫大量数据却只取几行，"
                    "优先看「扫描行数 Top」那一段。"
                )
        return notes


def parse(rows: list[tuple[int, str]]) -> dict:
    queries = split_queries(rows)
    report = Report()
    report.total = len(queries)

    for query in queries:
        if not query.sql:
            continue
        report.hist.add(query.moment)
        report.query_time_total += query.query_time
        report.rows_examined_total += query.rows_examined
        report.rows_sent_total += query.rows_sent

        template = normalize_sql(query.sql)
        if not template:
            continue

        # key = 归一化模板（归并依据），label 里才挂上时间数字。
        # 把 `12.481s` 拼进 key 是个陷阱：每条耗时都不同，于是同一条 SQL
        # 会以"12.481s SELECT…/8.220s SELECT…"的形式各占一行，榜单直接废掉。
        report.slowest.add(
            template, query.lineno, query.sql[:400],
            label=f"{query.query_time:.3f}s  {template[:160]}",
            metric=query.query_time,
        )
        report.scanners.add(
            template, query.lineno, query.sql[:400],
            label=f"{query.rows_examined:,} 行  {template[:150]}",
            metric=float(query.rows_examined),
        )
        if query.lock_time > 0:
            report.locks.add(
                template, query.lineno, query.sql[:400],
                label=f"{query.lock_time:.3f}s  {template[:150]}",
                metric=query.lock_time,
            )
        if query.rows_examined > 0:
            ratio = query.rows_examined / max(1, query.rows_sent)
            if ratio >= 100:
                report.worst_ratio.add(
                    template, query.lineno, query.sql[:400],
                    label=f"{ratio:,.0f}×  {template[:140]}",
                    metric=ratio,
                )
        report.databases.add(query.db or "(未知库)", query.lineno, "")
        report.users.add(query.user or "(未知用户)", query.lineno, "")

    return report.to_dict()


# ------------------------------------------------------------------ 下钻

def rows_for(report: dict, key: str, value: str) -> dict:
    return {"kind": KIND, "field": key, "value": value}
