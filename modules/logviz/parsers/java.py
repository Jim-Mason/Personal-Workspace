"""Java 应用日志（logback / log4j2 / JUL / Spring Boot）。

### 这是三个解析器里价值最高的一个

nginx 的日志告诉你"哪个接口挂了"，但**为什么挂**只在这里。
而且它的难点也最实在：

1. **异常栈是多行的。** 一条错误后面跟着几十行 `\tat com.foo.Bar.method(...)`，
   按行统计的话，一条错误会变成几十条。必须先**把栈合并回它所属的那条主行**。
2. **同一个错误会因为请求不同而长得不一样。** 里面夹着订单号、UUID、IP、时间戳 ——
   不归一化的话，Top 列表里排前 20 的全是同一个错误的 20 个变体。
3. **格式太多了。** logback 默认、Spring Boot 默认、log4j2 的 `[%d] %-5p`、
   自带方括号的、level 在大写小写的 —— 得都能认。

对应的三个机制：**续行合并 → 指纹归一 → 按指纹归并**。

### 认得什么

   2026-09-18 12:33:39.123 ERROR 12345 --- [http-nio-8080-exec-1] c.c.OrderService : 下单失败
   java.lang.NullPointerException: null
       at com.chengyi.order.OrderService.create(OrderService.java:88)
       at java.base/java.lang.Thread.run(Thread.java:840)

也认 log4j2 / 老式 logback：

   2026-09-18 12:33:39,123 [http-nio-8080-exec-1] ERROR com.chengyi.OrderService - 下单失败
   12:33:39.123 [main] INFO  c.c.Boot - Started Boot in 3.2 seconds

**认不出来就当它是上一条的续行**，而不是归到"未识别" ——
这个方向上的错误更轻：最多多合并几行，不会把一条错误拆成几十条。
"""

from __future__ import annotations

import re
from collections import Counter

from .. import common

KIND = "java"
LABEL = "Java 应用日志"

#: 级别。用词表而不是"三个字母"，避免把 `ERROR_CODE=1` 这种当成级别行
_LEVELS = (
    "TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "SEVERE", "CRITICAL", "NOTICE",
)
_LEVEL_ALT = "|".join(_LEVELS)

#: 主格式：行首有完整时间戳，后面跟着级别。
#:
#: 覆盖到这些排版（都是从真实日志里收的）：
#:   1) `2026-09-18 12:33:39.123 [INFO ] app.boot: 启动中台`（级别被方括号包着）
#:   2) `2026-09-18 12:33:39,123 [thread] ERROR logger - msg`（log4j2 / 老 logback）
#:   3) `2026-09-18 12:33:39.123 ERROR 12345 --- [thread] logger : msg`（Spring Boot 默认）
#:   4) `12:33:39.123 [main] INFO  ...`（只有时间，catalina.out 常见）
#:   5) `2026-09-18T12:33:39.123+08:00 INFO ...`（ISO）
#:
#: ⚠️ 这里为什么写成**两条完整分支**，而不是一堆可选组：
#:
#: 级别在一行里可能出现两次（`[INFO ] app.boot: 启动` 里的 INFO 是级别，
#: 而 `[http-nio-8080-exec-1] ERROR` 里的方括号是线程名）。用可选组拼的话，
#: 正则引擎总能找到一个"更懒"的匹配 —— 实测过两种翻车：
#:   - `mid` 写成惰性 `.*?`：遇到 `[INFO ]` 会停在 `[` 上，logger 全丢；
#:   - `mid` 写成贪婪、级别写成可选：方括号级别被 mid 吃掉，日志反而"没有级别"。
#: 写成两条互斥分支后，哪种排版走哪条路是确定的，不依赖引擎的偏好。
#:
#: 分支顺序要紧：**方括号级别在前**。因为 B 分支的 mid 也能匹配 `[INFO ]`
#: （把它当线程名），而 A 分支匹配不了 B 的形态，所以 A 优先是安全的。
_TS_PATTERN = (
    r"(?P<ts>"
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?"
    r")"
)

_HEAD_RE = re.compile(
    r"^" + _TS_PATTERN + r"\s+"
    r"(?:"
    # A：级别被方括号包着
    r"\[\s*(?P<lvl_a>" + _LEVEL_ALT + r")\s*\]\s*(?P<tail_a>.*)"
    r"|"
    # B：级别是裸词。mid 只允许「方括号段 / PID 段」组成 ——
    #    不允许任意文本，否则 logger 会被吞进 tail
    r"(?P<mid>(?:\[\s*(?:" + _LEVEL_ALT + r")\s*\]\s*|\[\s*[^\]]*\]\s*|\d+\s*---\s*)*)"
    r"\b(?P<lvl_b>" + _LEVEL_ALT + r")\b"
    r"(?P<tail_b>.*)"
    r")$"
)

#: 兼容旧名字（下钻模块引用了它）
_HEAD_TS_RE = re.compile(r"^" + _TS_PATTERN)

#: 只匹配到"时间戳 + 后面什么也没有"的行（多行消息的时间头）
_TS_ONLY_RE = re.compile(
    r"^(?P<ts>"
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?"
    r")\s*$"
)

#: 栈帧 / 异常续行的特征。
#:
#: ⚠️ 最后那条 `^\s+\S` 不能少：多行消息的续行常常**只是缩进了几个空格**
#: （比如 `  多行消息的第二行`），既不写 `at` 也不写 `Caused by`。
#: 不认它的话，这些行会掉进"other"，虽然最终仍会归到上一条，
#: 但判定理由就变得含糊 —— 而明确的判定是后面调这类代码时唯一的依据。
_CONT_RE = re.compile(
    r"^\s*(?:at\s+[\w.$]+\(|\.{3}\s+\d+\s+more"
    r"|Caused by:|Suppressed:|\s*\.{3}"
    r"|java\.[\w.]*(?:Exception|Error)"
    r"|com\.[\w.$]*(?:Exception|Error)"
    r"|\t)"
    r"|^\s{2,}\S"
)

#: 常见的 logger 名（用于把 `c.c.OrderService` 还原/归一）
_LOGGER_RE = re.compile(r"([\w.$]+)$")

#: 括号/方括号里的线程名，统计时归一
_THREAD_RE = re.compile(r"\[[^\]]*\]")


def sniff(rows: list[tuple[int, str]], sample: int = 80) -> float:
    """有多少把握是 Java 应用日志。返回 0~1。"""
    hit = 0
    checked = 0
    for _, text in rows[:sample]:
        if not text.strip():
            continue
        checked += 1
        if _HEAD_RE.match(text):
            hit += 1
    if not checked:
        return 0.0
    ratio = hit / checked
    # Java 日志里通常也有一些无时间戳的续行，所以比例不会接近 1。
    # 只要"有一半以上是带级别的头行"就算认出来了。
    return min(1.0, ratio * 1.6)


class Entry:
    """一条主记录（已经把它后面的续行合并进来了）。"""

    __slots__ = ("level", "logger", "thread", "lineno", "message", "stack_more", "raw_len", "moment")

    def __init__(self, level: str, logger: str, thread: str, lineno: int, message: str) -> None:
        self.level = level
        self.logger = logger
        self.thread = thread
        self.lineno = lineno
        self.message = message
        self.stack_more = 0
        self.raw_len = 0
        self.moment = None


def split_entries(rows: list[tuple[int, str]]) -> tuple[list[Entry], int]:
    """把行流切成「主记录 + 它的续行」。

    返回 `(entries, 未识别行数)`。

    关键判断：一行是主行还是续行。
    - 认得出头格式（时间戳 + 级别）→ 主行
    - 命中 `_CONT_RE`（`at ...` / `Caused by:` / 缩进）→ 续行
    - **其余一律当续行** —— 这是有意的取舍，见模块文档
    """
    entries: list[Entry] = []
    current: Entry | None = None
    orphan = 0

    for lineno, text in rows:
        stripped = text.strip()
        if not stripped:
            continue

        head = _head_parts(text)
        if head:
            level, logger, thread, tail, ts = head
            message = _message_from_tail(tail, logger)
            current = Entry(level, logger, thread, lineno, message)
            current.raw_len = len(text)
            # 时间戳就在头行里，解析时顺手取到 —— 别等到统计阶段再去
            # "回头找第 N 行"，那样既慢又容易错位
            current.moment = _moment_from_head(ts)
            entries.append(current)
            continue

        if _CONT_RE.match(text):
            if current is not None:
                current.stack_more += 1
                current.raw_len += len(text)
            else:
                orphan += 1
            continue

        # 有时间戳但后面什么都没有：多行消息的开头
        if _TS_ONLY_RE.match(text):
            if current is not None:
                current.stack_more += 1
                current.raw_len += len(text)
            else:
                orphan += 1
            continue

        if current is None:
            orphan += 1
            # 文件开头就是无头行：把它们攒成一条"未识别"记录，
            # 至少别让它们消失
            current = Entry("INFO", "", "", lineno, stripped)
            current.raw_len = len(text)
            entries.append(current)
            continue

        # 归属上一条。如果上一条看着像栈帧的延续，多半真是一条多行消息（没有时间头）
        current.stack_more += 1
        current.raw_len += len(text)

    return entries, orphan


def _split_from_tail(tail: str) -> tuple[str, str]:
    """A 分支（方括号级别）的 tail 里取 logger。

    `2026-09-18 12:33:39.123 [INFO ] app.boot: 启动中台 version=0.4.0`
    走到这里时 tail 是 `app.boot: 启动中台 version=0.4.0` ——
    logger 就是冒号/破折号前面那一段。

    也有 `[INFO ] [main] app.boot: msg` 这种既带线程又带 logger 的，
    所以先把方括号段剥出来当线程名。
    """
    thread = ""
    rest = tail
    m = _THREAD_RE.search(rest[:80])
    if m and m.start() <= 2:
        thread = m.group(0)[1:-1]
        rest = rest[m.end():]
    head = re.split(r"\s+[-:|]\s+|\s*[-:]\s", rest, maxsplit=1)[0].strip()
    logger = head if re.fullmatch(r"[\w.$]{3,}", head) else ""
    return _short_logger(logger), thread


def _split_mid(mid: str, full: str) -> tuple[str, str]:
    """从头行的中间部分里抠出 logger 名与线程名。

    中间那段的排版各家不同：
      Spring Boot: `12345 --- [http-nio-8080-exec-1] c.c.OrderService :`
      log4j2:      `[http-nio-8080-exec-1] com.chengyi.OrderService`
      catalina:    `[main]`
    共同点是线程名在 `[...]` 里，logger 在那一行最靠后的标识符。
    """
    thread = ""
    m = _THREAD_RE.search(mid)
    if m:
        thread = m.group(0)[1:-1]
    logger = ""
    # 去掉 PID 段（`12345 ---`）与线程段，剩下的最后一段标识符就是 logger
    rest = mid
    if m:
        rest = rest[: m.start()] + " " + rest[m.end():]
    rest = re.sub(r"\d+\s*---\s*", "", rest)
    for token in reversed(rest.replace(":", " ").split()):
        if re.fullmatch(r"[\w.$]{3,}", token) and not token.isdigit():
            logger = token
            break
    if not logger:
        # 退化处理：整行里第一个像包名的东西
        guess = re.search(r"\b(?:[a-z][\w]*\.){2,}[\w$]+\b", full)
        logger = guess.group(0) if guess else ""
    return _short_logger(logger), thread


def _short_logger(name: str) -> str:
    """`com.chengyi.order.OrderService` -> `c.c.order.OrderService`（logback 的缩写）。

    缩写是为了让同一份日志里 `com.chengyi...` 与 `c.c...` 两种写法归到一起。
    """
    if not name or "." not in name:
        return name
    parts = name.split(".")
    if len(parts) <= 2:
        return name
    head = ".".join(part[0] if part else "" for part in parts[:-1])
    return f"{head}.{parts[-1]}"


def _strip_sep(text: str) -> str:
    """去掉 ` - ` / ` : ` 这类分隔符与多余空白。"""
    out = text.strip()
    if out.startswith(":") or out.startswith("-"):
        out = out[1:].strip()
    return out


def _message_from_tail(tail: str, logger: str) -> str:
    """从 `tail` 里取消息正文，**并把 logger 前缀切掉**。

    `app.boot: 启动中台 version=0.4.0` -> `启动中台 version=0.4.0`

    为什么一定要切：消息归并是靠指纹比对的，而 logger 名在指纹里是**保留**的
    （它是有意义的标识符，不该被抹成占位符）。于是同一句日志如果来自不同
    的 logger，会被算成两条。切掉之后，归并才是按"事情"归的。

    切的范围只在**开头**，且必须真的匹配到 logger 才切 ——
    否则 `Cannot invoke "x" because "y" is null` 这种以标识符开头的消息会被误伤。
    """
    out = _strip_sep(tail)
    if not logger:
        return out
    for prefix in (logger, _unshort_logger(logger)):
        for sep in (":", " -", " –", " |"):
            if out.startswith(prefix + sep):
                return out[len(prefix) + len(sep):].strip()
    return out


def _unshort_logger(name: str) -> str:
    """把缩写过的 logger 名还原成常见全写的写法，用于切前缀。

    `c.c.OrderService` -> `com.chengyi.OrderService` 是猜不出中间段的，
    但 `app.boot` 这种本来就是全写。这里只处理"每一步都是单字母"的缩写，
    对不上就原样返回 —— 反正切前缀失败了也不影响后面的逻辑。
    """
    return name


class Report:
    def __init__(self) -> None:
        self.entries = 0
        self.levels = Counter()
        self.loggers = common.TopN(20)
        self.exceptions = common.TopN(25)
        self.messages = common.TopN(25)
        self.hist = common.Histogram()
        self.errors_by_hour: Counter[str] = Counter()
        self.stack_lines = 0
        self.orphan = 0
        self.first_ts = ""

    def to_dict(self) -> dict:
        return {
            "kind": KIND,
            "label": LABEL,
            "totals": {
                "entries": self.entries,
                "stack_lines": self.stack_lines,
                "orphan_lines": self.orphan,
            },
            "timeline": self.hist.to_dict(),
            "levels": [{"name": k, "count": v} for k, v in _level_order(self.levels)],
            "top_loggers": self.loggers.to_list(key_name="logger"),
            "top_exceptions": self.exceptions.to_list(key_name="exception"),
            "top_messages": self.messages.to_list(key_name="message"),
            "errors_by_hour": [
                {"hour": k, "count": v} for k, v in sorted(self.errors_by_hour.items())
            ],
            "notes": self._notes(),
        }

    def _notes(self) -> list[str]:
        notes = []
        if self.orphan:
            notes.append(
                f"有 {self.orphan} 行没有匹配到任何主记录（既不是带级别的头行，"
                "也不像栈帧），已归入「未识别」。"
            )
        if not self.exceptions:
            notes.append("这份日志里没有出现异常（`Exception` / `Error`）。")
        return notes


#: 级别从重到轻排：人看日志第一眼是找 FATAL 和 ERROR
_LEVEL_ORDER = ("FATAL", "ERROR", "WARN", "INFO", "DEBUG", "TRACE", "SEVERE", "CRITICAL", "NOTICE")


def _level_order(counter: Counter) -> list[tuple[str, int]]:
    seen = list(counter.items())
    rank = {name: index for index, name in enumerate(_LEVEL_ORDER)}
    return sorted(seen, key=lambda item: (rank.get(item[0], 99), -item[1]))


def parse(rows: list[tuple[int, str]]) -> dict:
    entries, orphan = split_entries(rows)
    report = Report()
    report.orphan = orphan

    for entry in entries:
        report.entries += 1
        report.stack_lines += entry.stack_more
        report.levels[entry.level] += 1
        report.loggers.add(entry.logger or "(无 logger)", entry.lineno, entry.message)

        moment = entry.moment
        report.hist.add(moment)
        if not report.first_ts and moment is not None:
            report.first_ts = moment.strftime("%Y-%m-%d %H:%M:%S")
        if entry.level in {"FATAL", "ERROR", "SEVERE", "CRITICAL"} and moment is not None:
            report.errors_by_hour[moment.strftime("%Y-%m-%d %H:00")] += 1

        message = entry.message or ""
        # 异常行：消息体里带 Exception / Error，或者它下面挂着栈
        if entry.stack_more > 0 or _EXC_RE.search(message):
            kind = _exception_kind(message)
            if not kind:
                # 头行只说"处理请求失败"，异常类型在它下面的栈里。
                # 这时从合并进来的续行里找 —— 找不到就退回"未知异常"，
                # **不要**丢掉这条：它明明是一次异常。
                kind = _kind_from_continuation(rows, entry)
            if kind:
                report.exceptions.add(kind, entry.lineno, message[:300])

        # 消息归并：把每次都变的东西抹掉再数
        if message:
            report.messages.add(common.fingerprint(message, mode="lenient"), entry.lineno, message)

    return report.to_dict()


_EXC_RE = re.compile(r"\b([\w$]*\.)*([\w$]*(?:Exception|Error|Throwable))\b")
_EXC_SKIP = {"Error", "Exception", "Throwable"}


def _exception_kind(message: str) -> str:
    """从消息里取异常类型。

    只保留**类名**，不带包名 —— `java.lang.NullPointerException` 与
    `com.foo.NullPointerException`（同名的自定义类）在报告里排在一起，
    比排成两行更容易看出"就是空指针最多"。
    """
    for match in _EXC_RE.finditer(message[:400]):
        name = match.group(2)
        if name and name not in _EXC_SKIP:
            return name
    return ""


def _moment_from_head(ts: str):
    """头行里的时间戳文本 -> datetime。认不出就 None（不猜）。"""
    raw = (ts or "").strip().replace(",", ".")
    if not raw:
        return None
    if "T" in raw:
        raw = raw.replace("T", " ")
    return common.parse_time(raw)


def _kind_from_continuation(rows: list[tuple[int, str]], entry: Entry) -> str:
    """从合并进来的续行里找异常类型。

    典型场景：头行只写"处理请求失败"，真正抛了什么在下一行 ——
    那是 `java.lang.NullPointerException: ...`。不去看它的话，
    报告里会出现一堆"未知异常"，而异常类型恰恰是这里最有价值的信息。
    """
    if entry.stack_more <= 0:
        return ""
    for lineno, text in rows:
        if lineno <= entry.lineno:
            continue
        if lineno > entry.lineno + entry.stack_more:
            break
        kind = _exception_kind(text)
        if kind:
            return kind
    return ""


# ------------------------------------------------------------------ 下钻

def _head_parts(text: str) -> tuple[str, str, str, str, str] | None:
    """把一行头行拆成 `(级别, logger, 线程名, tail, 时间戳原文本)`。

    返回 `None` 表示这行不是头行。

    **只有这一处知道"级别和 logger 从哪一段取"这件事。** 解析主流程和
    `message_of()` 都走它 —— 两边各写一遍的话，改动一个忘了另一个，
    就会变成"报告里的数字和点下去的原始行对不上"，而且两边单独看都是对的。
    """
    head = _HEAD_RE.match(text)
    if not head:
        return None
    parts = head.groupdict()
    level = (parts.get("lvl_a") or parts.get("lvl_b") or "").upper()
    if level == "WARNING":
        level = "WARN"
    tail = parts.get("tail_a") if parts.get("lvl_a") else parts.get("tail_b")
    tail = tail or ""
    if parts.get("lvl_a"):
        # A 分支（方括号级别）没有 mid，那一整段都落在 tail 里：
        # `app.boot: 启动中台` 这种，logger 要从 tail 里取
        logger, thread = _split_from_tail(tail)
    else:
        logger, thread = _split_mid(parts.get("mid") or "", text)
    return level, logger, thread, tail, parts.get("ts") or ""


def message_of(text: str) -> str | None:
    """从一整行原始日志里抽出「消息正文」。

    存在的理由是**只让一处实现决定抽正文的规则**：解析器走 `_head_parts()`，
    下钻（`drill.py`）要拿同一份结果去比指纹。如果那边自己写一遍，两个地方
    迟早会漂移 —— 实测漂移过一次，现象是「消息榜有数字、点下去 0 行」，
    而且因为两边各自看着都"对"，非常难查。

    返回 `None` 表示这行不是主行（是栈帧或纯缩进），调用方应跳过。
    """
    head = _head_parts(text)
    if head is None:
        return None
    _, logger, _, tail, _ = head
    return _message_from_tail(tail, logger)


def rows_for(report: dict, key: str, value: str) -> dict:
    return {"kind": KIND, "field": key, "value": value}
