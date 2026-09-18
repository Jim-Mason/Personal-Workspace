"""nginx access 日志。

### 认得什么

标准 combined / main 格式：

    127.0.0.1 - - [18/Sep/2026:12:33:39 +0800] "GET /api/apps HTTP/1.1" 200 1234 "-" "curl/8.0"

也容忍几种常见的变体：
- 行首有真实 IP（不在 X-Forwarded-For 里）时，`-` 少一个也不影响
- 上游是 `$remote_addr $remote_user [$time_local] "$request" $status $body_bytes_sent`
  这种 main 格式（没有 referer / ua）—— 后面两段缺了就用空值
- 带 `$request_time` / `$upstream_response_time` 的扩展格式（放在引号后）

### 为什么这个解析器最好做

它的**结构是固定的**：字段之间用空格和引号分隔，不需要"猜行类型"。
所以 report 里的数字最可信 —— 这也是把它放在第一类的理由。

### 报告里有什么

| 指标 | 回答什么问题 |
|---|---|
| 时间直方图 | 流量在什么时候起来、有没有断档 |
| 状态码分布 | 有多少是 5xx、多少是 4xx |
| Top URL | 哪个接口被调得最多 |
| Top IP | 谁在打 |
| Top UA | 是浏览器还是爬虫 |
| 慢请求 | 哪些请求最慢（**要日志里有 `$request_time` 才有**，没有就如实说没有） |
| 流量概览 | 总共回了多少字节 |

每个指标都带 `anchor_line`，点一下跳到原始行。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from .. import common

#: 主格式：行首 IP + [时间] + "请求行" + 状态码 + 字节数
#:
#: referer / UA 那段是可选的 —— main 格式没有这两段，扩展格式才有。
#: 这里用 `(?:\s+"..."\s+"...")?` 而不是把 `rest` 写成贪婪的 `.*`：
#: 写贪婪的话，一个 **main 格式的行**（"curl/8.0" 那种裸 UA）会被塞进 rest 里，
#: 于是 UA 统计全是空的。宁可认不出 referer/UA，也不要张冠李戴。
_LINE_RE = re.compile(
    r"^(?P<ip>\S+)\s+\S+\s+(?P<user>\S+)\s+"
    r"\[(?P<time>[^\]]+)\]\s+"
    r'"(?P<request>[^"]*)"\s+'
    r"(?P<status>\d{3})\s+"
    r"(?P<bytes>\d+|-)"
    r'(?:(?:\s+"(?P<referer>[^"]*)")?\s+"(?P<ua>[^"]*)")?'
    r"(?P<rest>.*)$"
)

#: 扩展字段里的耗时值。`$request_time` / `$upstream_response_time` 都是小数秒。
#:
#: 为什么要 `(?<![\d.])` 前视否定：写在 `$request` 后面的扩展字段里，
#: 也常常带 `$body_bytes_sent` 这种整数，甚至 `0.021 0.018` 连着两个。
#: 只取带小数点的、且不是某个数的一部分。
_EXTRA_RE = re.compile(r"(?<![\d.])(?P<value>\d+\.\d{1,6})(?![\d])")

#: 请求行：METHOD PATH PROTOCOL
_REQUEST_RE = re.compile(r"^(?P<method>[A-Z]+)\s+(?P<path>\S+)(?:\s+(?P<proto>\S+))?$")

#: 状态码分组：`(上界, 名字)`，判断方式是 `value < 上界`。
#:
#: ⚠️ 上界要写**下一档的起点**（2xx 的上界是 300，不是 299）。
#: 第一版写成了 `(200, "2xx 成功")`，于是 200 落在 `200 < 200 == False`，
#: 被归进"3xx 跳转"—— 整张状态码分布图每一行都错了一档，
#: 而数字本身看着完全正常。这类"错得整齐"的 bug 只能靠断言抓。
_STATUS_GROUP = (
    (300, "2xx 成功"),
    (400, "3xx 跳转"),
    (500, "4xx 客户端错"),
    (600, "5xx 服务端错"),
)

KIND = "nginx"
LABEL = "nginx access"


def sniff(rows: list[tuple[int, str]], sample: int = 60) -> float:
    """这份文件有多少把握是 nginx access 日志。返回 0~1。"""
    hit = 0
    checked = 0
    for _, text in rows[:sample]:
        if not text.strip():
            continue
        checked += 1
        if _LINE_RE.match(text):
            hit += 1
    if not checked:
        return 0.0
    return hit / checked


class Report:
    def __init__(self) -> None:
        self.total = 0
        self.parsed = 0
        self.unparsed: list[int] = []
        self.hist = common.Histogram()
        self.status = Counter()
        self.method = Counter()
        self.urls = common.TopN(25)
        self.ips = common.TopN(25)
        self.uas = common.TopN(15)
        self.slow = common.TopN(25)
        self.bytes_total = 0
        self.bytes_by_status: dict[str, int] = {}
        self.has_request_time = False
        self.max_request_time = 0.0
        self.first_line = 0
        self.last_line = 0

    def to_dict(self) -> dict:
        # 状态码按数值排，不按出现次数 —— 人看这个图是找"哪一类出了问题"，
        # 顺序稳定比"最多的排前面"更有用
        status_rows = [
            {
                "code": code,
                "group": _group(str(code)),
                "count": count,
                "anchor_line": self._status_anchor.get(code, 0),
                "bytes": self.bytes_by_status.get(str(code), 0),
            }
            for code, count in sorted(self.status.items())
        ]
        return {
            "kind": KIND,
            "label": LABEL,
            "totals": {
                "requests": self.total,
                "parsed": self.parsed,
                "unparsed": len(self.unparsed),
                "bytes": self.bytes_total,
            },
            "timeline": self.hist.to_dict(),
            "status_codes": status_rows,
            "methods": [{"name": k, "count": v} for k, v in self.method.most_common()],
            "top_urls": self.urls.to_list(key_name="url"),
            "top_ips": self.ips.to_list(key_name="ip"),
            "top_uas": self.uas.to_list(key_name="ua"),
            "slow_requests": self.slow.to_ranked(key_name="request") if self.has_request_time else [],
            "max_request_time": round(self.max_request_time, 3) if self.has_request_time else None,
            "notes": self._notes(),
        }

    def _notes(self) -> list[str]:
        notes = []
        if not self.has_request_time:
            notes.append(
                "这份 access 日志里没有 `$request_time` 字段，所以**没有慢请求排行**。"
                "在 nginx 的 log_format 里加上 `$request_time` 才会有。"
            )
        if self.unparsed:
            preview = ", ".join(str(n) for n in self.unparsed[:8])
            notes.append(
                f"有 {len(self.unparsed)} 行不符合 access 格式（行号 {preview}"
                + ("…" if len(self.unparsed) > 8 else "") + "），已跳过。"
            )
        return notes

    # 状态码 -> 首次出现行号（下钻锚点）。key 是状态码字符串，与 status 计数器同型
    _status_anchor: dict[str, int]


def _group(code: str) -> str:
    try:
        value = int(code)
    except ValueError:
        return "其它"
    for limit, name in _STATUS_GROUP:
        if value < limit:
            return name
    return "其它"


def parse(rows: list[tuple[int, str]]) -> dict:
    report = Report()
    report._status_anchor = {}

    for lineno, text in rows:
        if not text.strip():
            continue
        report.total += 1
        if not report.first_line:
            report.first_line = lineno
        report.last_line = lineno

        match = _LINE_RE.match(text)
        if not match:
            if len(report.unparsed) < 50:
                report.unparsed.append(lineno)
            continue

        report.parsed += 1
        moment = common.parse_nginx_time(match.group("time"))
        report.hist.add(moment)

        status = match.group("status")
        report.status[status] += 1
        # ⚠️ key 用**字符串**（与 `status` 一致），不要 `int()` 一下。
        # 第一版在这里 `int(status)`，而 to_dict 里拿的是字符串 `code`，
        # 于是查表永远落空 —— 现象是"状态码数字全对、下钻锚点全是 0"，
        # 报告看着完全正常，点下去才发现跳不动。
        report._status_anchor.setdefault(status, lineno)

        size = common.parse_size(match.group("bytes") or "0")
        report.bytes_total += size
        report.bytes_by_status[status] = report.bytes_by_status.get(status, 0) + size

        request = match.group("request") or ""
        parts = _REQUEST_RE.match(request)
        if parts:
            report.method[parts.group("method")] += 1
            # 去掉查询串再统计：`/api/apps?page=1` 与 `?page=2` 是同一个接口
            path = parts.group("path").split("?", 1)[0]
            report.urls.add(path, lineno, request)
        elif request:
            report.urls.add(request[:120], lineno, request)

        report.ips.add(match.group("ip"), lineno, text)
        ua = (match.group("ua") or "").strip()
        if ua and ua != "-":
            report.uas.add(_short_ua(ua), lineno, ua)

        # `$request_time` 这类扩展字段只可能是小数秒，裸整数是字节数之类的，
        # 所以只认带小数点的那些。
        #
        # ⚠️ 只扫 `rest`（引号之后那一段），**绝不回到整行里找** ——
        # 状态码和字节数都是裸数字，在整行里找小数会把它们算成耗时。
        # 而这个坑的可怕之处在于：它只在"恰好有小数"时才出错，平时看着是对的。
        extra = match.group("rest") or ""
        times = [float(m.group("value")) for m in _EXTRA_RE.finditer(extra)]
        if times:
            report.has_request_time = True
            cost = max(times)
            # key 用请求本身（不含耗时）—— 同一个接口被调多次时能归并；
            # 耗时挂在 label 与 metric 上。见 common.TopN 里那段说明。
            request_key = request[:120] or "(空请求)"
            report.slow.add(
                request_key, lineno, text,
                label=f"{cost:.3f}s  {request[:90]}",
                metric=cost,
            )
            if cost > report.max_request_time:
                report.max_request_time = cost

    return report.to_dict()


def _short_ua(ua: str) -> str:
    """UA 串太长了，归成"是什么东西"比原样列出来有用。"""
    lower = ua.lower()
    for token, name in (
        ("curl", "curl"),
        ("wget", "wget"),
        ("postman", "Postman"),
        ("python-requests", "python-requests"),
        ("python-urllib", "python-urllib"),
        ("java/", "Java"),
        ("okhttp", "OkHttp"),
        ("go-http-client", "Go http"),
        ("apache-httpclient", "Apache HttpClient"),
        ("httpx", "httpx"),
        ("chrome", "Chrome"),
        ("firefox", "Firefox"),
        ("safari", "Safari"),
        ("edge", "Edge"),
        ("msie", "IE"),
        ("bot", "爬虫"),
        ("spider", "爬虫"),
        ("crawler", "爬虫"),
        ("prometheus", "Prometheus"),
        ("zabbix", "Zabbix"),
    ):
        if token in lower:
            return name
    return ua[:60]


# ------------------------------------------------------------------ 下钻

def rows_for(report: dict, key: str, value: str) -> dict:
    """给"点某个指标"准备好查询参数。

    这里只做**参数翻译**，真正去读原始行的是 `drill.py` ——
    解析层不该再去碰文件。
    """
    mapping = {
        "url": ("url", value),
        "ip": ("ip", value),
        "ua": ("ua", value),
        "status": ("status", value),
        "request": ("request", value),
    }
    field, want = mapping.get(key, (key, value))
    return {"kind": KIND, "field": field, "value": want}
