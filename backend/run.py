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
from app.main import app as fastapi_app
from app.security import build_entry_url
from app.services import branding


def main() -> int:
    config.ensure_dirs()
    applied = run_migrations()
    token = config.get_or_create_token()
    url = build_entry_url(token)
    # 横幅显示用户自己设的名字，而不是出厂默认值
    display_name = branding.load().get("app_name") or config.APP_NAME

    line = "=" * 64
    print(line)
    print(f"  {display_name} v{config.APP_VERSION}  ·  个人工作台中台")
    print(line)
    print(f"  机器标识 : {config.get_or_create_machine_id()}")
    print(f"  数据目录 : {config.DATA_DIR}")
    if applied:
        print(f"  已应用迁移: {', '.join(applied)}")
    print(f"  访问地址 : {url}")
    print("-" * 64)
    print("  仅监听 127.0.0.1，局域网内其它设备无法访问")
    print("  按 Ctrl+C 停止服务")
    print(line)

    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(fastapi_app, host=config.HOST, port=config.PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
