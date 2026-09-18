#!/usr/bin/env python3
"""中台提供的标准启动壳：Flask / Flask-SocketIO。

**为什么需要这个文件（这是本方案最关键的一处设计）**

OpsGen 上游的入口是：

    socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)

三处都要改：
- `host="0.0.0.0"` —— 绑到所有网卡，局域网里任何人都能直连这个内部端口，
  等于绕开中台的所有鉴权与审计
- `debug=True` —— Werkzeug 调试器带交互式控制台，本身就是一条现成的远程命令执行链
- `allow_unsafe_werkzeug=True` —— 允许在生产进程里跑开发服务器

但「第三方代码不动源码」是硬约定（要能随时 `git pull` 同步上游）。
所以改法不是去改它，而是**根本不执行它的 `__main__` 分支**：
中台用自己的壳去 `import` 它的 app 对象，再由壳自己决定怎么起服务。

`import app` 只会触发模块级代码（建 Flask 对象、注册路由），
不会跑到 `if __name__ == "__main__":` 下面去 —— 监听地址因此天生由壳说了算。

壳不接受任何「用哪个地址」的参数：地址在代码里写死。就算有人往环境变量里
塞 HOST，也改不动它。
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
    socketio_object_name = os.environ.get("LOCALDECK_MODULE_SOCKETIO_OBJECT", "").strip()

    if not port_raw.isdigit():
        _fail("缺少 LOCALDECK_MODULE_PORT —— 这个壳只由中台拉起，不要手动执行")
    port = int(port_raw)

    # 模块自己的包（engine / services / …）都在它自己的目录下，
    # 上游是 `python app.py` 这么跑的，所以工作目录得进 sys.path。
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.chdir(root)

    print(f"[中台启动壳] 模块 {module_id}，监听 {HOST}:{port}（地址由中台接管）", flush=True)

    try:
        module = importlib.import_module(app_module_name)
    except Exception as exc:  # noqa: BLE001
        _fail(
            f"导入 {app_module_name}.py 失败：{type(exc).__name__}: {exc}\n"
            f"       最常见的原因是依赖没装在模块自己的 .venv 里。"
            f"       到项目根目录跑 scripts\\setup-modules.bat，或在模块面板点「装依赖」。"
        )
        return 2

    application = getattr(module, app_object_name, None)
    if application is None:
        _fail(f"{app_module_name}.py 里没有 {app_object_name!r} 这个对象")
        return 2

    # ---- Flask-SocketIO：用 socketio.run，但要按中台的意思传参
    if socketio_object_name:
        socketio = getattr(module, socketio_object_name, None)
        if socketio is None:
            _fail(f"{app_module_name}.py 里没有 {socketio_object_name!r} 这个对象")
            return 2
        try:
            socketio.run(
                application,
                host=HOST,
                port=port,
                debug=False,               # 调试器不开，它本身是 RCE 入口
                use_reloader=False,
                allow_unsafe_werkzeug=True,  # 本机单用户场景，够用且省一层部署
            )
        except TypeError:
            # 老版本的 flask-socketio 不认 allow_unsafe_werkzeug
            socketio.run(application, host=HOST, port=port, debug=False, use_reloader=False)
        return 0

    # ---- 普通 Flask
    if hasattr(application, "run"):
        application.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True)
        return 0
    if callable(application):
        # 兜底：任意 WSGI callable，直接交给 werkzeug
        from werkzeug.serving import run_simple

        run_simple(HOST, port, application, threaded=True, use_reloader=False)
        return 0

    _fail(f"{app_object_name!r} 既不是 Flask 应用也不是可调用的 WSGI 对象")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
