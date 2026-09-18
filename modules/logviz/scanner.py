"""流式扫描：把一个大文件按行喂给解析器，并守住预算。

### 为什么要单独一层

解析器本身只关心"这一行长什么样"，不该关心"读到第几 MB 了、要不要停"。
把预算、编码、断行这三件事收到这里，解析器就干净了，也能被单测直接调用。

### ⛔ 只读

用 `open(path, "r", ...)` —— 只有读模式，没有第二个参数能变成写。
不删、不移、不改名、不截断、不轮转。**任何情况下不回写一个字节。**

### 预算（防的是"点一下就卡死"）

日志文件动辄几百 MB 到几个 G，浏览器那头还等着响应。所以三层上限：

| 上限 | 值 | 到顶后的行为 |
|---|---|---|
| 字节 | `MAX_BYTES` | 停止读取，`truncated=True` |
| 行数 | `MAX_LINES` | 停止读取，`truncated=True` |
| 耗时 | `TIME_BUDGET` 秒 | 停止读取，`truncated=True` |

**到顶就如实说没读完**，绝不假装这份报告是全量的 —— 报告下面会写
"只读了前 X MB / 共 Y MB"，数字对不上时用户一眼能看出来。

### 编码

Windows 上的日志什么编码都有（UTF-8、GBK、还有混着 BOM 的）。
一律 `errors="replace"` 读进来，坏字节变成 U+FFFD —— 总比整个文件读不出来强。
如果替换字符占比过高，说明编码猜错了，扫描结束时把这件事**写进报告的
`notes`**，而不是让用户对着满屏乱码猜。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

#: 单次分析最多读这么多字节（200 MB）
MAX_BYTES = 200 * 1024 * 1024
#: 单次分析最多读这么多行（200 万行）
MAX_LINES = 2_000_000
#: 单次分析最多花这么多秒
TIME_BUDGET = 20.0
#: 单行最长保留这么多字符（超长行多半是一整份堆栈或一个巨型 JSON）
MAX_LINE_CHARS = 4000


class ScanResult:
    """一次扫描的账本：读了什么、读到哪、有没有读完。"""

    __slots__ = (
        "lines",
        "bytes_read",
        "file_size",
        "truncated",
        "reason",
        "elapsed",
        "bad_chars",
        "total_chars",
    )

    def __init__(self, file_size: int) -> None:
        self.lines: list[tuple[int, str]] = []  # (原始行号, 行内容)
        self.bytes_read = 0
        self.file_size = file_size
        self.truncated = False
        self.reason = ""
        self.elapsed = 0.0
        self.bad_chars = 0
        self.total_chars = 0

    @property
    def coverage(self) -> float:
        if self.file_size <= 0:
            return 1.0
        return min(1.0, self.bytes_read / self.file_size)

    def to_dict(self) -> dict:
        return {
            "lines": len(self.lines),
            "bytes_read": self.bytes_read,
            "file_size": self.file_size,
            "coverage": round(self.coverage, 4),
            "truncated": self.truncated,
            "reason": self.reason,
            "elapsed": round(self.elapsed, 3),
            "encoding_suspect": self.total_chars > 0 and self.bad_chars / self.total_chars > 0.02,
        }


def stream_lines(path: Path) -> ScanResult:
    """把文件读成 `[(行号, 文本), ...]`，并守住预算。

    逐行 `readline` 而不是 `readlines()`：后者会把整个文件塞进内存，
    几百 MB 的文件直接把服务顶爆。
    """
    result = ScanResult(_safe_size(path))
    started = time.monotonic()
    deadline = started + TIME_BUDGET

    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            lineno = 0
            while True:
                if time.monotonic() > deadline:
                    result.truncated = True
                    result.reason = f"读满 {TIME_BUDGET:.0f} 秒的预算"
                    break
                raw = handle.readline()
                if not raw:
                    break
                lineno += 1
                result.bytes_read += len(raw.encode("utf-8", "replace"))
                if lineno > MAX_LINES:
                    # 这一行已经读进来了但不算数，把行号退回去更诚实
                    lineno -= 1
                    result.truncated = True
                    result.reason = f"读满 {MAX_LINES:,} 行的预算"
                    break
                text = raw.rstrip("\r\n")
                if len(text) > MAX_LINE_CHARS:
                    text = text[:MAX_LINE_CHARS] + " …（本行过长，已截断）"
                result.bad_chars += text.count("\ufffd")
                result.total_chars += len(text)
                result.lines.append((lineno, text))
                if result.bytes_read > MAX_BYTES:
                    result.truncated = True
                    result.reason = f"读满 {MAX_BYTES // (1024 * 1024)} MB 的预算"
                    break
    except PermissionError:
        result.reason = "没有权限读这个文件"
    except OSError as exc:
        result.reason = f"读不了这个文件：{exc}"

    result.elapsed = time.monotonic() - started
    return result


def tail_lines(path: Path, count: int = 2000) -> list[tuple[int, str]]:
    """只读文件末尾若干行（下钻抽屉里"看看最新发生了什么"用）。

    从尾部按块倒着读，不去读整个文件 —— 一个 2 GB 的文件想看最后 50 行，
    没有理由先把 2 GB 过一遍。
    """
    try:
        size = _safe_size(path)
    except OSError:
        return []
    if size <= 0:
        return []

    block = 64 * 1024
    collected: list[bytes] = []
    newlines = 0
    read_bytes = 0
    try:
        with open(path, "rb") as handle:
            position = size
            while position > 0 and newlines <= count:
                step = min(block, position)
                position -= step
                handle.seek(position)
                chunk = handle.read(step)
                collected.append(chunk)
                newlines += chunk.count(b"\n")
                read_bytes += step
                if read_bytes > 8 * 1024 * 1024:
                    break
    except OSError:
        return []

    blob = b"".join(reversed(collected))
    text = blob.decode("utf-8", errors="replace")
    rows = text.splitlines()
    tail = rows[-count:] if len(rows) > count else rows
    # 行号只能估算：真实行号得从头数一遍，那正是我们要避免的。
    # 这里给的是「从末尾倒推」的序号，界面上会标明是估算值。
    start = max(1, _approximate_line_count(path, size) - len(tail) + 1)
    return [(start + index, line) for index, line in enumerate(tail)]


def _approximate_line_count(path: Path, size: int) -> int:
    """估个总行数：抽样算平均行长，再拿文件大小除。

    只用于给"看末尾"的行号一个大致位置。报告里的行号全部来自
    `stream_lines()` 的真实计数。
    """
    if size <= 0:
        return 0
    sample = min(size, 256 * 1024)
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(sample)
    except OSError:
        return 0
    if not chunk:
        return 0
    lines_in_sample = chunk.count(b"\n") or 1
    avg = len(chunk) / lines_in_sample
    if avg <= 0:
        return 0
    return int(size / avg)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_around(path: Path, center: int, context: int = 12) -> list[tuple[int, str]]:
    """读第 `center` 行附近的那一段（下钻用）。

    这个必须从头读到 `center`，因为文本文件没有行索引；但只保留窗口内的行，
    内存占用是常数级。超过预算就返回空列表 —— 宁可说"定位不到"，
    也不要为了它把服务卡 20 秒。
    """
    if center <= 0:
        return []
    start = max(1, center - context)
    end = center + context
    rows: list[tuple[int, str]] = []
    deadline = time.monotonic() + 6.0
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            lineno = 0
            for raw in handle:
                lineno += 1
                if lineno > end:
                    break
                if lineno >= start:
                    rows.append((lineno, raw.rstrip("\r\n")[:MAX_LINE_CHARS]))
                if lineno % 20000 == 0 and time.monotonic() > deadline:
                    return []
    except OSError:
        return []
    return rows


def iter_pairs(result: ScanResult) -> Iterator[tuple[int, str]]:
    return iter(result.lines)


def collect(result: ScanResult, fn: Callable[[int, str], None]) -> None:
    for lineno, text in result.lines:
        fn(lineno, text)


def as_rows(items: Iterable) -> list:
    return list(items)
