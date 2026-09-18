"""日志：给排障留一条路。

为什么要有这个文件 —— 之前服务的日志只往 stdout 丢，而且 uvicorn 是按
`log_level="warning"` 起的，**连访问日志都没开**。于是出问题的时候，
唯一的信息来源就是浏览器里那条红条，而红条只讲"失败了"，不讲"为什么"。

现在所有日志同时写 `data/logs/localdeck.log`。三条硬规矩：

1. **不记令牌明文、不记 Cookie 值、不记请求体。**
   日志文件是明文躺在磁盘上的，而且它比 `data/.token` 更容易被顺手发出去
   （贴给别人看、进截图）。凭据类的东西一律只记"有没有""对不对"。

2. **不记原始查询串。**
   模块入口地址形如 `/opsgen/?_t=<票据>`，记整条 URL 等于把票据写进日志；
   `/?token=<令牌>` 同理。所以只记路径 + 参数**名字**，值一概不落盘。

3. **磁盘要有上限。**
   轮转留 14 个文件、每个 20MB，最多 280MB。日志自己把磁盘吃满是最讽刺的故障。
"""

from __future__ import annotations

import logging
import logging.handlers
import time

from . import config

# 中台自己的日志都挂在这个名字下，方便以后按名字调级别。
LOGGER_NAME = "localdeck"

_FORMAT = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"
_MAX_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 14

_configured = False


class _MillisecondFormatter(logging.Formatter):
    """带毫秒的本地时间。

    默认 formatter 只能给到秒，而排障时最常问的是"这几件事谁先谁后" ——
    同秒内的顺序看不出来。毫秒这三个字符很便宜，值。
    """

    def formatTime(self, record, datefmt=None):  # noqa: N802 — 覆写父类的既有名字
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        return f"{stamp}.{int(record.msecs):03d}"


def setup_logging() -> logging.Logger:
    """装好日志，返回中台自己的 logger。重复调用是安全的。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    config.ensure_dirs()
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = _MillisecondFormatter(_FORMAT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        str(config.LOG_PATH),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,  # 第一次真要写才建文件，避免空跑也留个空文件
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 终端只放 WARNING 以上：启动横幅是 print 出来的，别让请求日志把它淹了。
    # 人要看细节就去看日志文件，那才是给它准备的地方。
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.WARNING)
    root.addHandler(console)

    # ---- 把"会替你把凭据写进日志"的第三方库压下去
    #
    # 这条是实测踩出来的，不是预防性写的：httpx 在 INFO 级会把**完整请求 URL**
    # 记下来，包括查询串 ——
    #
    #   httpx: HTTP Request: GET http://127.0.0.1/opsgen/?_t=<票据> "HTTP/1.1 200 OK"
    #   httpx: HTTP Request: GET http://127.0.0.1/inventory/api/apps?token=<令牌> "…"
    #
    # 中台代理模块时正是用 httpx，而模块入口的票据、以及任何走查询串的凭据，
    # 都会这样原样落进明文日志文件。上面那三条规矩管得住我自己写的代码，
    # 管不住依赖库 —— 所以这里直接把它们的入口堵上。
    #
    # 压到 WARNING 而不是完全静音：这些库真出问题时（连接失败、协议错误）
    # 照样要有声音，只是正常运行时闭嘴。
    for noisy in ("httpx", "httpcore", "uvicorn.access", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return logger


def describe_rotation() -> str:
    """给横幅用的一句话，让人知道日志有多大、在哪、能留多久。"""
    return f"{_MAX_BYTES // (1024 * 1024)} MB × {_BACKUP_COUNT} 个后轮转覆盖"
