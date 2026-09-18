"""解析器共用的零件：归并、直方图、Top N。

三个解析器要的东西高度重合 —— 都是"把很多条归一成一个指纹，再数哪个多"。
把这些抽出来，各自的文件里就只剩"这一行长什么样"的知识。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Iterable

# ------------------------------------------------------------------ 直方图

#: 时间直方图最多分这么多格。再多浏览器上就看不清了，而且图例也放不下
MAX_BUCKETS = 60


class Histogram:
    """按时间分桶的计数器。

    桶的粒度是**按跨度自动选的**，不是固定的分钟/小时 ——
    一份 10 分钟的日志和一份 3 天的日志，用同一个粒度都会难看。
    """

    def __init__(self) -> None:
        self.stamps: list[datetime] = []

    def add(self, moment: datetime | None) -> None:
        if moment is not None:
            self.stamps.append(moment)

    def to_dict(self) -> dict | None:
        if not self.stamps:
            return None
        low, high = min(self.stamps), max(self.stamps)
        span = (high - low).total_seconds()
        if span <= 0:
            # 全挤在同一秒里（或只有一条带时间的记录）
            return {
                "start": low.strftime("%Y-%m-%d %H:%M:%S"),
                "end": high.strftime("%Y-%m-%d %H:%M:%S"),
                "step_seconds": 1,
                "step_label": "1 秒",
                "buckets": [{"t": low.strftime("%H:%M:%S"), "n": len(self.stamps)}],
                "total": len(self.stamps),
            }
        step = _pick_step(span)
        slots = int(span // step) + 1
        while slots > MAX_BUCKETS:
            step *= 2
            slots = int(span // step) + 1
        counts = [0] * slots
        for stamp in self.stamps:
            index = int((stamp - low).total_seconds() // step)
            if 0 <= index < slots:
                counts[index] += 1
        buckets = []
        for index, count in enumerate(counts):
            moment = low.timestamp() + index * step
            buckets.append({"t": datetime.fromtimestamp(moment).strftime(_fmt_for(step)), "n": count})
        return {
            "start": low.strftime("%Y-%m-%d %H:%M:%S"),
            "end": high.strftime("%Y-%m-%d %H:%M:%S"),
            "step_seconds": step,
            "step_label": _label_for(step),
            "buckets": buckets,
            "total": len(self.stamps),
        }


def _pick_step(span: float) -> int:
    """挑一个"人看着顺眼"的步长（1/5/10/30 秒、1/5/10/30 分、1/6/12 小时、1 天）。"""
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
                 7200, 10800, 21600, 43200, 86400):
        if span / step <= MAX_BUCKETS:
            return step
    return 86400


def _label_for(step: int) -> str:
    if step < 60:
        return f"{step} 秒"
    if step < 3600:
        return f"{step // 60} 分钟"
    if step < 86400:
        return f"{step // 3600} 小时"
    return f"{step // 86400} 天"


def _fmt_for(step: int) -> str:
    if step < 60:
        return "%H:%M:%S"
    if step < 86400:
        return "%m-%d %H:%M"
    return "%m-%d"


# ------------------------------------------------------------------ Top N


class TopN:
    """带下钻锚点的 Top 计数器。

    记的不只是"有多少条"，还有**第一条出现在哪一行** ——
    报告里点一下要能跳到原始行，没有这个锚点就只能干瞪眼。

    ### `key` 与 `label` 必须分开传

    有的榜单需要在名字前面挂一个数字（`12.481s  SELECT ...`）。如果把这个
    装饰过的整串当 key，**每条的时间都不一样**，于是本该归并成一行的
    "同一条 SQL 的不同实例"会各占一行，Top 榜就废了。

    所以：`key` 是归并依据（归一化后的 SQL / URL…），
    `label` 只是给人看的显示文本。不传 label 就用 key。
    """

    def __init__(self, size: int = 20) -> None:
        self.size = size
        self.counter: Counter[str] = Counter()
        self.anchor: dict[str, int] = {}
        self.sample: dict[str, str] = {}
        self.label: dict[str, str] = {}
        #: 每个 key 的关键度量（耗时 / 扫描行数…），用于榜单里显示"最坏的那次"
        self.metric: dict[str, float] = {}

    def add(self, key: str, lineno: int, raw: str = "", *, label: str = "", metric: float | None = None) -> None:
        if not key:
            return
        self.counter[key] += 1
        if key not in self.anchor:
            self.anchor[key] = lineno
            self.sample[key] = raw[:400]
            self.label[key] = label or key
        # 度量取**最大值**：榜单要回答"哪条最坏"，不是"哪条平均最坏"
        if metric is not None and metric > self.metric.get(key, float("-inf")):
            self.metric[key] = metric

    def to_list(self, *, key_name: str = "value") -> list[dict]:
        rows = []
        for value, count in self.counter.most_common(self.size):
            item = {
                key_name: value,
                "count": count,
                "anchor_line": self.anchor.get(value, 0),
                "sample": self.sample.get(value, ""),
            }
            display = self.label.get(value, "")
            if display and display != value:
                item["label"] = display
            if value in self.metric:
                item["metric"] = self.metric[value]
            rows.append(item)
        return rows

    def to_ranked(self, *, key_name: str = "value") -> list[dict]:
        """按**度量**从大到小排的榜单（最慢的、扫得最多的…）。

        与 `to_list()` 的区别很要紧：那个是按"出现次数"排的。
        「最慢的 SQL」如果按出现次数排，排第一的会是"跑得挺快但跑了 1000 次"
        的那条 —— 那不是用户问的问题。**问什么就按什么排。**
        """
        rows = self.to_list(key_name=key_name)
        rows.sort(key=lambda item: (-item.get("metric", 0), -item["count"]))
        return rows

    def __len__(self) -> int:
        return len(self.counter)


# ------------------------------------------------------------------ 归一化

_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{16,}\b")
_LONG_NUM_RE = re.compile(r"\b\d{6,}\b")
_NUM_RE = re.compile(r"\b\d+\b")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_QUOTED_RE = re.compile(r"'[^']{4,}'|\"[^\"]{4,}\"")
#: 请求号 / 追踪号：bmw7x9k2、a1b2c3d4e5f6 这类
_TRACE_RE = re.compile(r"\b(?=[a-z0-9]{8,40}\b)(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)[a-z0-9]+\b")


def fingerprint(text: str, *, mode: str = "log") -> str:
    """把一条消息归一成"指纹"，让长得像的错误归到一起去。

    这是「相似错误归并」的核心。方法很朴素但很管用：**把每次都变的东西替换掉**。

    - 时间戳、UUID → `<ts>` / `<uuid>`
    - IP、邮箱、长十六进制串、长数字 → `<ip>` / `<mail>` / `<hex>` / `<num>`
    - 剩下的小数字也统一成 `<n>`
    - 引号里的内容 → `<str>`（mode="lenient" 时才做）

    为什么时间戳必须先换掉：如果两个错误只是发生时间不同，
    它们**就是同一个错误**，不该在 Top 列表里占两行。
    """
    out = _TS_RE.sub("<ts>", text)
    out = _UUID_RE.sub("<uuid>", out)
    out = _EMAIL_RE.sub("<mail>", out)
    out = _HEX_RE.sub("<hex>", out)
    if mode == "lenient":
        out = _QUOTED_RE.sub("<str>", out)
        out = _TRACE_RE.sub("<id>", out)
    out = _IP_RE.sub("<ip>", out)
    out = _LONG_NUM_RE.sub("<num>", out)
    out = _NUM_RE.sub("<n>", out)
    return out.strip()


# ------------------------------------------------------------------ 时间


def parse_time(text: str) -> datetime | None:
    """从一段文本里认出时间戳。认不出就返回 None（**不猜**）。"""
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt[0])
        except (ValueError, TypeError):
            continue
    return None


_TIME_FORMATS = (
    # ISO / Java / Spring Boot / log4j2 默认
    ("%Y-%m-%d %H:%M:%S.%f", None),
    ("%Y-%m-%d %H:%M:%S,%f", None),
    ("%Y-%m-%dT%H:%M:%S.%f", None),
    ("%Y-%m-%dT%H:%M:%S", None),
    ("%Y-%m-%d %H:%M:%S", None),
    ("%Y-%m-%d %H:%M", None),
    # nginx access 的 [18/Sep/2026:12:33:39 +0800]
    ("%d/%b/%Y:%H:%M:%S", None),
    ("%d/%b/%Y:%H:%M:%S %z", None),
    # MySQL 慢查询的 # Time: 2026-09-18T12:33:39.123456+08:00
    ("%Y-%m-%dT%H:%M:%S.%f%z", None),
    ("%Y-%m-%dT%H:%M:%S%z", None),
    # 只有时间没有日期（Tomcat 的 catalina.out 偶尔这样）
    ("%H:%M:%S.%f", None),
    ("%H:%M:%S", None),
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_nginx_time(text: str) -> datetime | None:
    """`18/Sep/2026:12:33:39 +0800` -> datetime（保留时区偏移）。"""
    m = re.match(
        r"(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})(?:\s*([+-]\d{4}))?",
        text.strip(),
    )
    if not m:
        return None
    day, mon_name, year, hour, minute, second, tz = m.groups()
    month = _MONTHS.get(mon_name.lower())
    if month is None:
        return None
    try:
        moment = datetime(int(year), month, int(day), int(hour), int(minute), int(second))
    except ValueError:
        return None
    if tz:
        # 归一化到本地时间：不带着时区往下传，直方图才有可比性
        sign = 1 if tz[0] == "+" else -1
        offset = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
        from datetime import timedelta

        moment = moment - timedelta(minutes=offset)
    return moment


def parse_size(text: str) -> int:
    """`1234` / `1.2K` / `3M` / `2.5G` -> 字节数。认不出返回 0。"""
    m = re.match(r"^\s*([\d.]+)\s*([KMGTP]?)B?\s*$", text or "", re.I)
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "").upper()
    scale = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    return int(value * scale.get(unit, 1))


def looks_like(row: str, *needles: str) -> bool:
    return all(needle in row for needle in needles)


def split_lines(rows: Iterable[tuple[int, str]]) -> list[str]:
    return [text for _, text in rows]
