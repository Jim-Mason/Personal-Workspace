"""OpsGen 在中台里的接入声明。

这个文件由中台读取，**OpsGen 自己的源码一行都不用改** —— 上游的
`app.py`、`engine/`、`services/`、`templates/` 全部保持原样，随时可以
`git pull` 同步上游版本。中台只负责：用自己的启动壳起它的进程（把
上游那个 `host="0.0.0.0"` + `debug=True` 换掉）、把 `/opsgen/*` 反代过去、
把返回页面里的根路径补成 `/opsgen/...`。

> 若上游将来自己也加了 `module.py`，本文件会与之冲突 —— 到时候按需
> 合并即可，中台侧不需要改动。
"""

MODULE = ModuleSpec(  # noqa: F821 - ModuleSpec 由中台在加载时注入，模块无需 import
    id="opsgen",
    name="运维脚本生成",
    description="问答式生成运维脚本：选模板 → 填参数 → 出脚本，可复制、下载、留历史",
    version="1.0",
    order=20,
    kind="subprocess_proxy",
    # ------------------------------------------------------------------ 启动
    # runner 由中台提供，不是这个模块里的文件。这样「监听地址、调试开关、
    # 超时、端口探测」这些安全相关的参数全部由中台说了算。
    runner="flask_app",
    app_module="app",       # import app
    app_object="app",       # Flask 应用对象
    socketio_object="socketio",  # 用 socketio.run 起（OpsGen 是 Flask-SocketIO）
    internal_port=8732,     # 只绑 127.0.0.1；紧挨中台的 8731，方便记
    health_path="/",
    startup_timeout=30.0,
    auto_start=True,
    # ------------------------------------------------------------------ 安全开关
    # 实时通道默认关。OpsGen 的 socket.io 上只有一个事件 `execute_script`，
    # 它在服务端把脚本写进临时文件然后 `bash` 执行 —— 这等于给浏览器开了一个
    # 任意命令执行入口。中台的安全边界里写明「不提供任意命令执行」，
    # 所以要开必须先配白名单 + 逐次确认 + 参数模板。
    #
    # 关掉后的表现：生成的脚本照常复制/下载，只是页面上的「▶️ 在线执行」
    # 按钮会明确告诉你为什么不能用 —— 不会静默失败。
    allow_realtime=False,
    # 外部 CDN 默认剥掉，对应「零网络上传、无遥测」。
    # OpsGen 的页面会从 cdnjs 引 highlight.js（代码高亮）和 socket.io。
    # 剥掉后：socket.io 由中台给的替身接管（见上），highlight.js 由空实现
    # 兜住，页面不会报错，只是代码块失去配色。想要配色就把它改成 True ——
    # 代价是页面打开时会向 cdnjs.cloudflare.com 发请求。
    allow_external_assets=False,
    # ------------------------------------------------------------------ 合规
    source_url="https://github.com/MMCISAGOODMAN/OpsGen",
    license="MIT（上游未附 LICENSE 文件，依其 README 声明）",
    note="上游 README 的出处声明必须持续保留；源码按原样放在本目录，便于 git pull 同步",
)
