"""下钻：从报告里的某个指标，回到文件里的原始行。

### 这是本模块存在的一半理由

报告回答"是什么情况"，但运维真正要动手时需要的永远是**具体哪几行**。
没有下钻的话，用户看完报告还得自己回去 grep，那这个工具就只做了一半。

### 实现上的取舍：重扫，不建索引

朴素做法是扫描时把每个指标的命中行号都存下来。但那意味着**内存开销跟
命中数量成正比** —— 一个满屏 404 的 access 日志，光"状态码 404 的行号"
就能有上百万个。

所以这里的选择是：**存指标与锚点，不存全部行号；下钻时重扫一遍**。
- 扫描一遍 200 MB 大约几秒，用户点一下等一下是可以接受的
- 内存占用是常数级，不随日志大小涨
- 复用同一套预算与解析器，不存在"索引和正文对不上"的隐患

代价是下钻会重读文件。用缓存挡了一层（同一个文件+同一个查询，
短时间内不重复扫），实际体感是够的。

### ⛔ 仍然是只读

下钻会打开文件，但**只读**。不缓存文件内容到磁盘（只有聚合结果进缓存库），
不写回原文件。
"""

from __future__ import annotations

import re
from pathlib import Path

from . import common, scanner

#: 一次下钻最多返回这么多行。再多前端也滚不动，而且说明筛选条件不够紧
MAX_ROWS = 300

#: 下钻命中行里，前后各带多少行上下文（让用户看到栈、看到前因后果）
CONTEXT = 0


def collect(
    path: Path,
    kind: str,
    field: str,
    value: str,
    *,
    limit: int = MAX_ROWS,
    case_sensitive: bool = False,
) -> dict:
    """把某个指标对应的原始行捞出来。

    返回的行带**真实行号** —— 用户拿着它可以直接去编辑器里跳。
    """
    result = scanner.stream_lines(path)
    rows = result.lines

    if field == "line":
        # 直接按行号取（报告里的锚点点进来时走这条）
        try:
            wanted = int(value)
        except (TypeError, ValueError):
            return _empty("行号不对", result)
        window = [row for row in rows if wanted - 4 <= row[0] <= wanted + 12]
        return {
            "field": field,
            "value": value,
            "rows": [{"line": n, "text": t, "hit": n == wanted} for n, t in window],
            "matched": len(window),
            "scanned": len(rows),
            "truncated": result.truncated,
            "reason": result.reason,
        }

    matcher = _matcher(kind, field, value, case_sensitive)
    if matcher is None:
        return _empty(f"这个模块不支持按 `{field}` 下钻", result)

    hits: list[dict] = []
    for lineno, text in rows:
        if matcher(lineno, text):
            hits.append({"line": lineno, "text": text, "hit": True})
            if len(hits) >= limit:
                break

    return {
        "field": field,
        "value": value,
        "rows": hits,
        "matched": len(hits),
        "scanned": len(rows),
        "capped": len(hits) >= limit,
        "limit": limit,
        "truncated": result.truncated,
        "reason": result.reason,
    }


def _empty(note: str, result: scanner.ScanResult) -> dict:
    return {
        "field": "",
        "value": "",
        "rows": [],
        "matched": 0,
        "scanned": len(result.lines),
        "truncated": result.truncated,
        "reason": result.reason,
        "note": note,
    }


def _matcher(kind: str, field: str, value: str, case_sensitive: bool):
    """给每种日志的每个指标造一个判定函数。

    这里**重新走一遍解析逻辑**，而不是拿报告里存的东西对 ——
    报告里存的是归一化之后的指纹，跟原始行对不上。
    唯一的例外是行号（`anchor_line`），它本来就是准确的。
    """
    flags = 0 if case_sensitive else re.IGNORECASE

    if kind == "nginx":
        from .parsers import nginx as nginx_parser

        def match(lineno: int, text: str) -> bool:
            m = nginx_parser._LINE_RE.match(text)
            if not m:
                return False
            if field == "status":
                return m.group("status") == value
            if field == "ip":
                return m.group("ip") == value
            if field == "url":
                request = m.group("request") or ""
                parts = nginx_parser._REQUEST_RE.match(request)
                path = parts.group("path").split("?", 1)[0] if parts else request
                return path == value
            if field == "ua":
                return nginx_parser._short_ua((m.group("ua") or "").strip()) == value
            if field == "request":
                return value in (m.group("request") or "")
            return False

        return match

    if kind == "java":
        from .parsers import java as java_parser

        if field == "level":
            def match_level(lineno: int, text: str) -> bool:
                head = java_parser._head_parts(text)
                return bool(head) and head[0] == value.upper()

            return match_level

        if field == "exception":
            def match_exc(lineno: int, text: str) -> bool:
                return value in text

            return match_exc

        if field == "logger":
            def match_logger(lineno: int, text: str) -> bool:
                head = java_parser._head_parts(text)
                return bool(head) and head[1] == value

            return match_logger

        if field == "message":
            # 报告里的指纹是**消息正文**的指纹（logger 前缀已经被解析器切掉了），
            # 而这里手上是一整行原始文本。所以不能直接对整行取指纹 ——
            # 那样算出来的是 `<ts> ERROR <n> --- [...] c.c.OrderService : 下单失败…`，
            # 跟报告里的 `<n> --- [...] … : 下单失败…` **永远对不上**，
            # 现象是"消息榜有数、点下去 0 行"。
            #
            # 正确做法是**重走一遍解析器抽正文的那条路**：头行 -> message -> 指纹。
            # 与报告对齐的不是"归一化规则"，而是"先抽正文、再归一化"这个次序。
            def match_message(lineno: int, text: str) -> bool:
                message = java_parser.message_of(text)
                if message is None:
                    return False
                return common.fingerprint(message, mode="lenient") == value

            return match_message

        # 兜底：当子串找
        def match_text(lineno: int, text: str) -> bool:
            return value.lower() in text.lower()

        return match_text

    if kind == "mysql":
        from .parsers import mysql as mysql_parser

        if field in {"sql", "request", "value"}:
            def match_sql(lineno: int, text: str) -> bool:
                # 报告里是归一化模板，所以原始行也要归一化后再比。
                # 但要两句两句地比 —— 一条慢查询的 SQL 可能跨好几行，
                # 单行归一化后未必等于整条的模板。
                single = mysql_parser.normalize_sql(text)
                if single and single in value:
                    return True
                return common.fingerprint(text, mode="lenient") == value

            return match_sql

        if field == "db":
            def match_db(lineno: int, text: str) -> bool:
                use = mysql_parser._USE_DB_RE.match(text.strip())
                return bool(use) and use.group("db") == value

            return match_db

        def match_any(lineno: int, text: str) -> bool:
            return value.lower() in text.lower()

        return match_any

    return None


def preview(path: Path, lines: int = 60, offset: int = 0) -> dict:
    """取文件开头若干行，用于"先看一眼它到底长什么样"。

    界面上有它很要紧：用户拿着一份不知道是什么的 `.out`，
    先看头几行就知道该选哪一类。
    """
    result = scanner.stream_lines(path)
    total = len(result.lines)
    start = max(0, min(offset, max(0, total - 1)))
    window = result.lines[start: start + lines]
    return {
        "rows": [{"line": n, "text": t, "hit": False} for n, t in window],
        "offset": start,
        "lines": lines,
        "total_lines": total,
        "file_size": result.file_size,
        "truncated": result.truncated,
        "reason": result.reason,
        "encoding_suspect": result.to_dict()["encoding_suspect"],
    }
