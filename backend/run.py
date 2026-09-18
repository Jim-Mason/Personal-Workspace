"""启动入口：跑迁移 → 打印访问地址 → 起服务 → 自动打开浏览器。

直接 `python run.py` 即可，不需要记 uvicorn 的任何参数。
加 `--no-browser` 可以只起服务不自动开浏览器。
"""

from __future__ import annotations

import sys
import threading
import webbrowser

import uvicorn

from app import config
from app.db import run_migrations
from app.logging_setup import setup_logging
from app.main import app as fastapi_app
from app.security import build_entry_url
from app.services import branding


def main() -> int:
    log = setup_logging()
    config.ensure_dirs()
    applied = run_migrations()
    if applied:
        # 迁移结果也要落日志：终端里那行字窗口一关就没了，而排障时最先要
        # 确认的就是「表结构到底对不对」—— 这句话必须留得下来。
        log.info("已应用迁移：%s", ", ".join(applied))
    token = config.get_or_create_token()
    url = build_entry_url(token)
    # 横幅显示用户自己设的名字，而不是出厂默认值
    display_name = branding.load().get("app_name") or config.APP_NAME
    machine = config.get_or_create_machine_id()

    line = "=" * 64
    print(line)
    print(f"  {display_name} v{config.APP_VERSION}  ·  个人工作台中台")
    print(line)
    print(f"  机器标识 : {machine}")
    print(f"  数据目录 : {config.DATA_DIR}")
    print(f"  运行日志 : {config.LOG_PATH}")
    if applied:
        print(f"  已应用迁移: {', '.join(applied)}")
    print(f"  访问地址 : {url}")
    print("-" * 64)
    print("  仅监听 127.0.0.1，局域网内其它设备无法访问")
    print(f"  上面的地址打开一次就会记住这个浏览器（{config.SESSION_TTL_DAYS} 天），")
    print(f"  之后直接访问 http://{config.HOST}:{config.PORT} 即可，不用再带令牌。")
    print("  按 Ctrl+C 停止服务")
    print(line)

    log.info(
        "启动中台 %s v%s｜机器 %s｜端口 %s｜数据目录 %s｜日志 %s",
        display_name,
        config.APP_VERSION,
        machine,
        config.PORT,
        config.DATA_DIR,
        config.LOG_PATH,
    )

    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        fastapi_app,
        host=config.HOST,
        port=config.PORT,
        # log_config=None：别让 uvicorn 再配一套自己的 handler，它的 logger
        # 会冒泡到 root，于是 uvicorn 自己的报错也一起进日志文件。
        # access_log=False：访问日志由 security._log_request 记，那份带耗时
        # 和**拒绝原因**，比 uvicorn 的详细；开着只是重复一遍。
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
