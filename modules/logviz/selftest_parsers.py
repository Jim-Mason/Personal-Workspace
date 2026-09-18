"""解析器自检：不需要中台在跑，直接喂样本给三个解析器。

为什么单独一个脚本：解析器最容易出的错不是"崩了"，而是**算错了数字还一脸自信**
（时间戳没认出来 → 直方图是空的；栈没合并 → 异常数虚高十倍；SQL 没归一化 →
Top 10 全是同一条语句）。这类错误跑一遍页面上看不出来，只能靠判据盯。

跑法：
    .venv\\Scripts\\python.exe modules\\logviz\\selftest_parsers.py

退出码 0 = 全部通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from logviz import common, drill, parsers  # noqa: E402
from logviz.parsers import java, mysql, nginx  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
rows: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    rows.append((PASS if ok else FAIL, name, detail))
    mark = "  [OK] " if ok else "  [!!] "
    print(f"{mark}{name}" + (f"  —— {detail}" if detail else ""))


# ------------------------------------------------------------------ nginx 样本
NGINX_SAMPLE = """\
172.16.0.9 - - [18/Sep/2026:12:00:01 +0800] "GET /api/apps HTTP/1.1" 200 1234 "-" "curl/8.0.1"
172.16.0.9 - - [18/Sep/2026:12:00:02 +0800] "GET /api/apps?page=2 HTTP/1.1" 200 2345 "-" "curl/8.0.1"
10.0.0.5 - - [18/Sep/2026:12:01:15 +0800] "POST /api/login HTTP/1.1" 401 88 "-" "Mozilla/5.0 (Windows NT 10.0) Chrome/120"
10.0.0.5 - - [18/Sep/2026:12:01:16 +0800] "POST /api/login HTTP/1.1" 401 88 "-" "Mozilla/5.0 (Windows NT 10.0) Chrome/120"
172.16.0.9 - - [18/Sep/2026:12:05:00 +0800] "GET /api/orders HTTP/1.1" 500 512 "-" "python-requests/2.31"
172.16.0.9 - - [18/Sep/2026:12:05:01 +0800] "GET /api/orders HTTP/1.1" 502 512 "-" "python-requests/2.31"
10.0.0.7 - - [18/Sep/2026:12:30:00 +0800] "GET /admin HTTP/1.1" 403 0 "-" "Mozilla/5.0 (X11; Linux) Firefox/121"
这是一行不符合格式的杂项内容
172.16.0.9 - - [18/Sep/2026:13:00:00 +0800] "GET /api/apps HTTP/1.1" 200 999 "-" "curl/8.0.1"
"""

NGINX_SLOW = """\
172.16.0.9 - - [18/Sep/2026:12:00:01 +0800] "GET /api/apps HTTP/1.1" 200 1234 "-" "curl/8.0" 0.021
172.16.0.9 - - [18/Sep/2026:12:00:02 +0800] "GET /api/report HTTP/1.1" 200 99999 "-" "curl/8.0" 8.412
172.16.0.9 - - [18/Sep/2026:12:00:03 +0800] "GET /api/orders HTTP/1.1" 200 10 "-" "curl/8.0" 3.150
"""

rows_in = [(i + 1, line) for i, line in enumerate(NGINX_SAMPLE.splitlines())]

print("=" * 72)
print(" nginx access 解析器")
print("=" * 72)

sniff_score = nginx.sniff(rows_in)
check("认得出一份 access 日志", sniff_score > 0.5, f"把握 {sniff_score:.2f}")

rep = nginx.parse(rows_in)
# 样本 9 行，其中 1 行是故意写的"不符合格式的杂项" → 8 行可解析。
# （这几个数字是数出来的。第一版我全凭印象写，结果让一份正确的实现
#   挂了 6 个红灯 —— 断言写错和实现写错一样有害。）
check("总行数统计正确", rep["totals"]["requests"] == 9,
      f"实际 {rep['totals']['requests']}（9 行非空）")
check("解析成功 8 行、1 行不合格式",
      rep["totals"]["parsed"] == 8 and rep["totals"]["unparsed"] == 1,
      f"parsed={rep['totals']['parsed']} unparsed={rep['totals']['unparsed']}")
check("不合格式的那行写进了 notes", any("不符合 access 格式" in n for n in rep["notes"]), "")

# 时间直方图：12:00–13:00 的跨度，应该分出来不止一格
check("时间直方图非空", rep["timeline"] is not None and rep["timeline"]["total"] == 8,
      f"{rep['timeline']['total'] if rep['timeline'] else 0} 条带时间（8 行可解析）")
check("直方图步长合理", rep["timeline"]["step_seconds"] >= 60,
      f"步长 {rep['timeline']['step_label']}")

# 状态码：200×3（两行 apps + 最后一行）、401×2、500、502、403
# ⚠️ key 是**字符串**（"200" 而不是 200）—— 与 JSON 里 code 字段同型。
# 第一版断言用了 int key，明明实现是对的却报 None，白折腾一轮。
codes = {item["code"]: item["count"] for item in rep["status_codes"]}
check("状态码统计正确",
      codes.get("200") == 3 and codes.get("401") == 2
      and codes.get("500") == 1 and codes.get("502") == 1,
      f"200={codes.get('200')} 401={codes.get('401')} 500={codes.get('500')} 403={codes.get('403')}")
check("状态码带分组名且分组正确",
      {i["code"]: i["group"] for i in rep["status_codes"]}.get("200") == "2xx 成功"
      and {i["code"]: i["group"] for i in rep["status_codes"]}.get("401") == "4xx 客户端错"
      and {i["code"]: i["group"] for i in rep["status_codes"]}.get("500") == "5xx 服务端错",
      "、".join(f"{i['code']}:{i['group']}" for i in rep["status_codes"]))
check("状态码带下钻锚点", all(item["anchor_line"] > 0 for item in rep["status_codes"]),
      "、".join(f"{i['code']}→L{i['anchor_line']}" for i in rep["status_codes"]))

# Top URL：/api/apps 出现了 3 次（两行带查询串 + 最后一行），查询串要去掉
urls = {item["url"]: item["count"] for item in rep["top_urls"]}
check("URL 统计去掉查询串后归并", urls.get("/api/apps") == 3,
      f"/api/apps = {urls.get('/api/apps')}（应为 3，含 ?page=2 那次）")

# Top IP
ips = {item["ip"]: item["count"] for item in rep["top_ips"]}
check("IP 统计正确", ips.get("172.16.0.9") == 5, f"172.16.0.9 = {ips.get('172.16.0.9')}")

# UA 归一
uas = {item["ua"]: item["count"] for item in rep["top_uas"]}
check("UA 归成可读名字",
      uas.get("curl") == 3 and uas.get("Chrome") == 2 and uas.get("python-requests") == 2,
      f"curl={uas.get('curl')} Chrome={uas.get('Chrome')} python-requests={uas.get('python-requests')}")

check("没有 $request_time 时如实说没有慢请求",
      rep["slow_requests"] == [] and any("request_time" in n for n in rep["notes"]), "")

# 字节总数
check("流量统计正确", rep["totals"]["bytes"] == 1234 + 2345 + 88 + 88 + 512 + 512 + 0 + 999,
      f"实际 {rep['totals']['bytes']}")

print()
print("=" * 72)
print(" nginx access —— 有 $request_time 的格式")
print("=" * 72)
slow_rows = [(i + 1, line) for i, line in enumerate(NGINX_SLOW.splitlines())]
rep2 = nginx.parse(slow_rows)
check("认出了 $request_time", rep2["slow_requests"] != [], "")
if rep2["slow_requests"]:
    top = rep2["slow_requests"][0]
    # 榜单按**耗时**排（不是按出现次数）。耗时挂在 metric 上、
    # 显示文本挂在 label 上 —— 断言两个都要看，只看 label 会漏掉"排错了"。
    check("慢请求排第一的是最慢那条",
          top.get("metric") == 8.412 and "8.412" in top.get("label", ""),
          f"metric={top.get('metric')} label={top.get('label', '')[:50]}")
    check("慢请求带下钻行号", top["anchor_line"] == 2, f"行号 {top['anchor_line']}（应为 2）")
    check("同一接口被调多次时归并成一行",
          all(item["count"] >= 1 for item in rep2["slow_requests"]),
          f"{len(rep2['slow_requests'])} 行榜单，全部有计数")

# ------------------------------------------------------------------ Java 样本
JAVA_SAMPLE = """\
2026-09-18 12:33:39.123 [INFO ] app.boot: 启动中台 version=0.4.0 port=8731
2026-09-18 12:33:40.001 [INFO ] app.http: GET /api/apps 200 12ms
2026-09-18 12:33:41.500 [WARN ] app.store: 幂等键冲突 source=inventory key=app:chrome
2026-09-18 12:33:42.100 [ERROR] app.handler: 处理请求失败 requestId=a1b2c3d4e5f6 orderId=88213
java.lang.NullPointerException: Cannot invoke "com.foo.Order.getId()" because "order" is null
\tat com.chengyi.order.OrderService.create(OrderService.java:88)
\tat com.chengyi.order.OrderController.post(OrderController.java:41)
\tat java.base/java.lang.Thread.run(Thread.java:840)
2026-09-18 12:33:45.700 [ERROR] app.handler: 处理请求失败 requestId=ff00aa11bb22 orderId=99101
java.lang.NullPointerException: Cannot invoke "com.foo.Order.getId()" because "order" is null
\tat com.chengyi.order.OrderService.create(OrderService.java:88)
\tat com.chengyi.order.OrderController.post(OrderController.java:41)
2026-09-18 12:34:01.000 [ERROR] app.db: 连接池耗尽 active=50 idle=0
Caused by: java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available
\tat com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:197)
2026-09-18 12:35:00.000 [INFO ] app.http: GET /api/health 200 3ms
\t这是没有时间头的续行，应该被合并
2026-09-18 12:40:00.000 [INFO ] app.boot: 这是一条只有半句的
  多行消息的第二行
  多行消息的第三行
"""

java_rows = [(i + 1, line) for i, line in enumerate(JAVA_SAMPLE.splitlines())]

print()
print("=" * 72)
print(" Java 应用日志解析器")
print("=" * 72)

jscore = java.sniff(java_rows)
check("认得出一份 Java 应用日志", jscore > 0.5, f"把握 {jscore:.2f}")

jrep = java.parse(java_rows)
entries, orphan = java.split_entries(java_rows)

# 样本里**恰好 8 行**以「时间戳 + 级别」开头（数一遍：L1,2,3,4,9,13,16,18），
# 其余 12 行是栈帧 / 多行消息的续行。这个数字是数出来的，不是估的 ——
# 之前写成 9 是我自己数错，反倒让一条正确的实现挂了红灯。
check("头行识别数量正确", len(entries) == 8,
      f"认出 {len(entries)} 条主记录（样本里 8 行带时间戳+级别）")
check("续行全部合并、没有孤儿", orphan == 0, f"孤儿 {orphan} 行")

# 关键判据：栈必须合并回主行，绝不能按行数变成 3 条异常
check("异常栈已合并回主记录（不是按行数）", jrep["totals"]["stack_lines"] >= 6,
      f"合并掉的续行 {jrep['totals']['stack_lines']} 行")

levels = {item["name"]: item["count"] for item in jrep["levels"]}
check("级别统计正确", levels.get("ERROR") == 3 and levels.get("INFO") == 4 and levels.get("WARN") == 1,
      f"INFO={levels.get('INFO')} WARN={levels.get('WARN')} ERROR={levels.get('ERROR')}")
check("级别按严重程度排序", jrep["levels"][0]["name"] == "ERROR",
      f"第一个是 {jrep['levels'][0]['name']}")

excs = {item["exception"]: item["count"] for item in jrep["top_exceptions"]}
check("异常按类名归并（只留类名不带包）",
      excs.get("NullPointerException") == 2,
      f"NullPointerException={excs.get('NullPointerException')}（样本里出现 2 次）")
check("认出了 SQLTransientConnectionException",
      excs.get("SQLTransientConnectionException") == 1, f"{list(excs.items())}")

# 关键判据：两行「处理请求失败」只差 requestId 与 orderId，必须归成同一条消息。
# 注意找的是**消息正文里的那句中文**，不是 NullPointerException ——
# 异常类型在下一行的栈里，不在头行的消息里。第一版断言写错了对象。
msgs = jrep["top_messages"]
dup = [m for m in msgs if "处理请求失败" in m.get("sample", "")]
check("同一条错误的不同变体归并成一条消息",
      len(dup) == 1 and dup[0]["count"] == 2,
      f"匹配到 {len(dup)} 条，count={dup[0]['count'] if dup else 0}（应为 1 条 / 2 次）")
check("消息的 key 是归一化指纹（不含具体 ID）",
      bool(dup) and "88213" not in dup[0]["message"] and "99101" not in dup[0]["message"],
      f"key：{dup[0]['message'] if dup else ''}")

# 异常类型在续行（栈）里，必须也能抓到 —— 这是"头行只说失败、原因在下一行"
# 这个常见形态的关键。抓不到的话报告里会全是"未知异常"。
check("异常类型能从续行里找回", excs.get("NullPointerException") == 2, f"{excs}")

check("时间直方图非空", jrep["timeline"] is not None and jrep["timeline"]["total"] == 8,
      f"{jrep['timeline']['total'] if jrep['timeline'] else 0} 条（8 条主记录各有一个时间戳）")
check("错误按小时分布", len(jrep["errors_by_hour"]) == 1 and jrep["errors_by_hour"][0]["count"] == 3,
      f"{jrep['errors_by_hour']}")

loggers = {item["logger"]: item["count"] for item in jrep["top_loggers"]}
check("logger 名统计到了", "app.handler" in loggers, f"前几个：{list(loggers.items())[:4]}")

# ------------------------------------------------------------------ MySQL 样本
MYSQL_SAMPLE = """\
# Time: 2026-09-18T12:00:01.123456+08:00
# User@Host: app[app] @  [172.16.0.9]  Id: 88213
# Query_time: 12.480921  Lock_time: 0.000213 Rows_sent: 3  Rows_examined: 4820113
SET timestamp=1758168001;
SELECT * FROM device_properties_message WHERE device_id = 88213 ORDER BY ts DESC LIMIT 3;
# Time: 2026-09-18T12:00:05.500000+08:00
# User@Host: app[app] @  [172.16.0.9]  Id: 88220
# Query_time: 8.220100  Lock_time: 0.000100 Rows_sent: 1  Rows_examined: 4820113
SET timestamp=1758168005;
SELECT * FROM device_properties_message WHERE device_id = 99101 ORDER BY ts DESC LIMIT 3;
# Time: 2026-09-18T12:02:00.000000+08:00
# User@Host: report[report] @  [172.16.0.12]  Id: 88400
# Query_time: 0.450000  Lock_time: 5.120000 Rows_sent: 100  Rows_examined: 100
SET timestamp=1758168120;
SELECT id, name FROM orders WHERE status IN ('NEW', 'PAID', 'SHIPPED') AND created_at > 1758100000;
"""

mysql_rows = [(i + 1, line) for i, line in enumerate(MYSQL_SAMPLE.splitlines())]

print()
print("=" * 72)
print(" MySQL 慢查询解析器")
print("=" * 72)

mscore = mysql.sniff(mysql_rows)
check("认得出一份慢查询日志", mscore >= 0.9, f"把握 {mscore:.2f}")

mrep = mysql.parse(mysql_rows)
check("按 # Time: 切成 3 块", mrep["totals"]["slow_queries"] == 3,
      f"实际 {mrep['totals']['slow_queries']}")
check("Query_time 求和正确", abs(mrep["totals"]["query_time_total"] - (12.480921 + 8.220100 + 0.450000)) < 0.001,
      f"合计 {mrep['totals']['query_time_total']}s")
check("扫描行数求和正确", mrep["totals"]["rows_examined_total"] == 4820113 * 2 + 100,
      f"{mrep['totals']['rows_examined_total']:,}")

# 关键判据：两条只差 device_id 的 SQL 必须归成同一条模板
slow = mrep["slowest"]
check("最慢的排第一", slow[0].get("metric") == 12.480921,
      f"Top1 metric={slow[0].get('metric')} label={slow[0].get('label', '')[:60]}")
check("同模板 SQL 归并（只差字面量）", slow[0]["count"] == 2,
      f"Top1 命中 {slow[0]['count']} 条（device_id 不同 → 应为 2）")

template = mysql.normalize_sql("SELECT * FROM t WHERE a = 1 AND b = 'x' AND c IN (1,2,3)")
check("SQL 归一化抹掉字面量、保留结构",
      "?" in template and "SELECT * FROM t WHERE a =" in template and "IN (?)" in template,
      f"→ {template}")

# 全表扫描那两条
scans = mrep["full_scans"]
check("扫描行数 Top 抓到全表扫描",
      scans[0].get("metric") == 4820113 and scans[0]["count"] == 2,
      f"Top1 metric={scans[0].get('metric')} count={scans[0]['count']}")

check("锁等待单独统计",
      mrep["locks"] != [] and mrep["locks"][0].get("metric") == 5.12,
      f"Top1 metric={mrep['locks'][0].get('metric') if mrep['locks'] else '(空)'}"
      f" label={mrep['locks'][0].get('label', '')[:50] if mrep['locks'] else ''}")

check("按库/按用户统计", len(mrep["users"]) >= 2, f"用户：{[u['user'] for u in mrep['users']]}")

check("扫描/返回比过高时给出提示", any("扫描行数 / 返回行数" in n for n in mrep["notes"]),
      f"{mrep['notes']}")

# ------------------------------------------------------------------ 类型识别
print()
print("=" * 72)
print(" 类型自动识别")
print("=" * 72)

d_nginx = parsers.detect(rows_in)
check("access 样本被判为 nginx", d_nginx["best"] == "nginx",
      f"best={d_nginx['best']} 分数={d_nginx['scores']}")
d_java = parsers.detect(java_rows)
check("Java 样本被判为 java", d_java["best"] == "java",
      f"best={d_java['best']} 分数={d_java['scores']}")
d_mysql = parsers.detect(mysql_rows)
check("慢查询样本被判为 mysql", d_mysql["best"] == "mysql",
      f"best={d_mysql['best']} 分数={d_mysql['scores']}")

junk = [(1, "随便写点什么"), (2, "这不是任何一类日志"), (3, "{json: true}")]
d_junk = parsers.detect(junk)
check("认不出来就如实说认不出来", d_junk["best"] == "",
      f"best='{d_junk['best']}' 分数={d_junk['scores']}")

# ------------------------------------------------------------------ 归一化
print()
print("=" * 72)
print(" 指纹归一化")
print("=" * 72)

a = common.fingerprint("2026-09-18 12:33:42.100 处理请求失败 requestId=a1b2c3d4e5f6 orderId=88213", mode="lenient")
b = common.fingerprint("2026-09-18 12:33:45.700 处理请求失败 requestId=ff00aa11bb22 orderId=99101", mode="lenient")
check("只差时间与 ID 的两条消息指纹相同", a == b, f"\n       A: {a}\n       B: {b}")

c = common.fingerprint("连接超时 host=172.16.0.9 port=5432", mode="lenient")
d = common.fingerprint("连接超时 host=10.0.0.5 port=3306", mode="lenient")
check("IP 与端口不同也归成一条", c == d, f"\n       C: {c}\n       D: {d}")

e1 = common.fingerprint("下单失败：库存不足", mode="lenient")
e2 = common.fingerprint("下单失败：余额不足", mode="lenient")
check("内容真的不同的消息不会被误并", e1 != e2, f"\n       E1: {e1}\n       E2: {e2}")

# ------------------------------------------------------------------ 下钻可达性
#
# 这一节盯的是一类**特别难查的** bug：报告里的数字是对的、下钻的查询也对，
# 但两边用的归一化口径不同，于是"点下去 0 行"。
#
# 实测踩过一次：报告里的 message key 是**消息正文**的指纹（logger 前缀已被
# 解析器切掉），而 drill 那边拿**整行**去取指纹 —— 算出来永远带一段
# `<ts> ERROR <n> --- [...]`，两边永远不可能相等。两边各自单独看都对，
# 只有把「报告里的 key」真的拿去喂「下钻的判定函数」才会暴露。
#
# 所以判据就写成这件事本身：**报告里存的每个 key，都必须能在原始行里找到。**
print()
print("=" * 72)
print(" 下钻可达性（报告里的 key 必须能在原始行里找到）")
print("=" * 72)


def reachable(kind, raw_rows, report, key_field, value, label_field):
    """报告里的某个 key，拿去跑一遍下钻判定，看能不能命中。"""
    matcher = drill._matcher(kind, key_field, value, False)
    if matcher is None:
        return 0
    return sum(1 for lineno, text in raw_rows if matcher(lineno, text))


# Java：消息榜第一名（就是那条归并了 2 次的"处理请求失败"）必须可达
if msgs:
    top = msgs[0]
    hits = reachable("java", java_rows, jrep, "message", top["message"], "message")
    check("Java 消息榜的 key 下钻得到行（抽正文的口径两侧一致）",
          hits == top["count"],
          f"key 命中 {hits} 行，报告里是 {top['count']} 次")

# Java：logger 榜
if jrep["top_loggers"]:
    top = jrep["top_loggers"][0]
    hits = reachable("java", java_rows, jrep, "logger", top["logger"], "logger")
    check("Java logger 榜的 key 下钻得到行", hits == top["count"],
          f"{top['logger']} 命中 {hits} 行，报告里是 {top['count']} 次")

# Java：级别
level_row = next((x for x in jrep["levels"] if x["name"] == "ERROR"), None)
if level_row:
    hits = reachable("java", java_rows, jrep, "level", "ERROR", "level")
    check("Java 级别榜的 key 下钻得到行", hits == level_row["count"],
          f"ERROR 命中 {hits} 行，报告里是 {level_row['count']} 次")

# nginx：URL 榜（`/api/apps` 的 query string 已被抹掉，下钻也要按抹掉的比）
nginx_rows, nrep = rows_in, rep
if nrep["top_urls"]:
    top = nrep["top_urls"][0]
    hits = reachable("nginx", nginx_rows, nrep, "url", top["url"], "url")
    check("nginx URL 榜的 key 下钻得到行", hits == top["count"],
          f"{top['url']} 命中 {hits} 行，报告里是 {top['count']} 次")

# nginx：状态码
status_row = next((x for x in nrep["status_codes"] if x["count"] >= 2), None)
if status_row:
    hits = reachable("nginx", nginx_rows, nrep, "status", str(status_row["code"]), "status")
    check("nginx 状态码榜的 key 下钻得到行", hits == status_row["count"],
          f"{status_row['code']} 命中 {hits} 行，报告里是 {status_row['count']} 次")

# MySQL：最慢 SQL（归一化模板，原始行要归一化后再比）
if mrep["slowest"]:
    top = mrep["slowest"][0]
    hits = reachable("mysql", mysql_rows, mrep, "sql", top["sql"], "sql")
    check("MySQL 最慢 SQL 的模板下钻得到行", hits == top["count"],
          f"模板命中 {hits} 行，报告里是 {top['count']} 次")

# 反向判据：**不存在的 key 必须一行都命中不到**。
# 少了这条，"判定函数永远返回 True"这种错误会被上面几条全部放过。
bogus = reachable("java", java_rows, jrep, "message", "<这个指纹不存在>", "message")
check("不存在的 key 命中 0 行（判定不是恒真）", bogus == 0, f"命中 {bogus} 行")

# ------------------------------------------------------------------ 汇总
print()
print("=" * 72)
failed = [r for r in rows if r[0] == FAIL]
print(f" 合计 {len(rows)} 项：通过 {len(rows) - len(failed)}，失败 {len(failed)}")
if failed:
    print(" 失败明细：")
    for _, name, detail in failed:
        print(f"   - {name}：{detail}")
print("=" * 72)
sys.exit(1 if failed else 0)
