#!/usr/bin/env python3
"""中台提供的标准启动壳：ASGI（FastAPI / Starlette / 任意 ASGI 应用）。

与 `flask_app.py` 是同一个目的 —— **让中台而不是模块来决定监听地址**：

- 端口来自 `LOCALDECK_MODULE_PORT`，地址在代码里写死 `127.0.0.1`
- 只 `import` 模块的 ASGI 对象，绝不执行它的 `__main__` 分支

**为什么不直接用 uvicorn 命令行？**

命令行会把 host / port 变成参数，而那正是要收口的东西：参数能被模块的 `.env`、
部署脚本、或某次手滑的命令行改掉。写死在壳里之后，`module.py` 里连「监听哪里」
这个字段都不存在，也就无从改起。任何往环境变量里塞 `HOST` 的尝试都改不动它。

**与 Flask 壳的两点差异**

1. 用 `uvicorn.run` 而不是 `app.run`；**worker 恒为 1**。SQLite 的写并发本来就有限，
   而且模块常在 lifespan 里跑数据库迁移与初始化 —— 多 worker 会并发跑同一份，
   属于自找的竞态。（`uvicorn.run` 传 app 对象时本来也不支持多 worker。）
2. `log_config=None` —— 不去覆盖模块自己的 logging 配置。很多模块（比如 portal）
   自己配了结构化 JSON 日志，而 uvicorn 默认的 log config 会把 root logger 的
   handler 顶掉，日志就散了。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

# 监听地址不允许来自环境变量或命令行。这是硬约束，改这里等于改安全边界。
HOST = "127.0.0.1"


def _fail(message: str) -> None:
    print(f"[中台启动壳] {message}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def main() -> int:
    root = Path(os.environ.get("LOCALDECK_MODULE_ROOT") or os.getcwd()).resolve()
    module_id = os.environ.get("LOCALDECK_MODULE_ID", root.name)
    port_raw = os.environ.get("LOCALDECK_MODULE_PORT", "")
    app_module_name = os.environ.get("LOCALDECK_MODULE_APP_MODULE", "app")
    app_object_name = os.environ.get("LOCALDECK_MODULE_APP_OBJECT", "app")
    sub_cwd = (os.environ.get("LOCALDECK_MODULE_CWD") or "").strip()

    if not port_raw.isdigit():
        _fail("缺少 LOCALDECK_MODULE_PORT —— 这个壳只由中台拉起，不要手动执行")
    port = int(port_raw)

    # 模块内的工作目录：给「Python 包藏在子目录里」的项目用。
    # portal 的包在 backend/ 下，上游是以 backend 为 cwd 跑 uvicorn 的，
    # 所以这里要先 chdir 过去、再把该目录插进 sys.path，否则 import 不到 app.main。
    workdir = (root / sub_cwd).resolve() if sub_cwd else root
    # 防御性检查：声明校验那边已经挡过一次，但这里是真正 chdir 的地方，
    # 不能让一个漏网的 app_cwd 把工作目录、进而把 sys.path 指到模块外面去。
    if workdir != root and root not in workdir.parents:
        _fail(f"app_cwd 指到了模块目录之外：{workdir}")
        return 2
    if not workdir.is_dir():
        _fail(f"工作目录不存在：{workdir} —— 检查 module.py 里的 app_cwd 写对没有")
        return 2
    if str(workdir) not in sys.path:
        sys.path.insert(0, str(workdir))
    os.chdir(workdir)

    print(
        f"[中台启动壳] 模块 {module_id}，ASGI，监听 {HOST}:{port}（地址由中台接管）",
        flush=True,
    )

    try:
        module = importlib.import_module(app_module_name)
    except Exception as exc:  # noqa: BLE001
        _fail(
            f"导入 {app_module_name} 失败：{type(exc).__name__}: {exc}\n"
            f"       最常见的原因是依赖没装在模块自己的 .venv 里，"
            f"或 app_cwd 指错了目录（当前 {workdir}）。\n"
            f"       到项目根目录跑 scripts\\setup-modules.bat，或在模块面板点「装依赖」。"
        )
        return 2

    application = getattr(module, app_object_name, None)
    if application is None:
        _fail(f"{app_module_name} 里没有 {app_object_name!r} 这个对象")
        return 2

    try:
        import uvicorn
    except ImportError:
        _fail(
            "模块环境里没有 uvicorn。\n"
            "       ASGI 模块需要它来起服务，请在模块的 requirements.txt 里加上 uvicorn。"
        )
        return 2

    uvicorn.run(
        application,
        host=HOST,
        port=port,
        # 中台就是那个反向代理：信自己生成的那份 X-Forwarded-*，来源限定回环。
        # （比上游部署脚本里的 forwarded-allow-ips='*' 更严：只信 127.0.0.1。）
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        # 不覆盖模块自己的 logging 配置，见文件头说明
        log_config=None,
        log_level=os.environ.get("LOCALDECK_MODULE_LOG_LEVEL", "info"),
        # auto：模块若在 lifespan 里跑迁移，必须让它执行
        lifespan="auto",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
