"""模块三：内网系统导航门户 + 反向代理网关（本项目自研，非第三方）。

接入形态选 `subprocess_proxy`（子进程 + 反代），三条理由：

1. 它是个**完整应用** —— 自带 SQLite、上传目录、SPA 产物和登录体系，
   塞进中台进程会把这些状态搅在一起
2. 它有 **WebSocket 代理**能力（`services/ws_proxy.py`），WSGI 跑不了，
   必须独立进程
3. 复用已经验证过的机制，模块侧零改造

`app_cwd="backend"` 是因为它的 Python 包在 `backend/` 下，上游部署脚本也是以
那个目录为工作目录跑 uvicorn 的（见 `deploy/start.sh`）。启动壳会 chdir 过去
并把它插进 sys.path。

### 两处偏离默认值的声明，都不是随手改的

**`allow_realtime=True`** —— 中台默认阻断模块的实时通道，因为 OpsGen 把
「在线执行脚本」挂在 socket.io 上，那等同于一个浏览器可触发的任意命令执行入口。
但 portal 的 WebSocket 是**代理到内网目标**的转发通道，风险性质完全不同：
它转发的流量受 `services/proxy_guard.py` 的目标白名单约束，不是让浏览器
在本机执行东西。所以这里显式放行。

**`allow_module_cookies=True`** —— portal 有自己的 JWT 登录体系。不放行的话
它连登录都登不上：登录接口刚 `Set-Cookie`，下一个请求的 Cookie 就被中台剥掉了。
放行的只是**它自己的** Cookie，中台票据（`pw_mod_*`）照样挡在外面。

### `ROOT_PATH` 是干什么的

portal 的后端在生成代理链接时会拼 `settings.root_path`（见 `proxy_service.py`）。
给它 `/portal`，它自己吐出来的链接就带上中台前缀，省掉一层事后改写 ——
这是「两层代理」（中台 → portal → 内网目标）里最关键的一环。

### `TRUST_LOCAL_PROXY` —— 挂在中台后面就不必再登录一次

中台本身已经把边界做全了（只绑 127.0.0.1 + Host 校验 + 访问令牌），
再让用户在自己的机器上敲一遍门户的登录页是纯粹的重复劳动。给它这个开关，
它就认可「经中台代理而来的请求」为已登录。

**凭据不是随便什么头**：中台转发时会带上 `X-LocalDeck-Module-Ticket`，
值是本模块自己的票据 —— 每次运行新生成的随机串，只在中台与模块之间流动。
portal 侧要求「开关打开 + 来源回环 + 票据一致」三条同时成立（见 `api/deps.py`
的 `local_trust_active`）。

**独立运行不受影响**：`deploy/start.sh` 那条路不经中台启动，环境变量不存在，
票据无从比对，只能正常登录。这个默认值是刻意的 —— 免登录的正当性完全来自
上游那道边界，边界不在就不能免。
"""

MODULE = ModuleSpec(
    id="portal",
    name="工作地址集合",
    description="内网系统导航门户：卡片式入口，点开即用；内置反向代理网关，可把内网页面直接嵌进来",
    version="1.0",
    order=30,
    kind="subprocess_proxy",
    runner="asgi_app",
    app_cwd="backend",
    app_module="app.main",
    app_object="app",
    internal_port=8733,
    health_path="/health",
    startup_timeout=45.0,
    allow_realtime=True,
    allow_module_cookies=True,
    allow_external_assets=False,
    env={"ROOT_PATH": "/portal", "TRUST_LOCAL_PROXY": "1"},
    # 依赖清单在 backend/ 下，不在模块根 —— 不写这句的话引导脚本会以为
    # 这个模块没有依赖可装
    requirements="backend/requirements.txt",
    license="自有项目（本项目自研，非第三方）",
    note=(
        "自带登录体系与 WebSocket 代理，所以放行了实时通道与模块自有 Cookie —— "
        "理由见本文件顶部注释。已开启 TRUST_LOCAL_PROXY：挂在中台后面时不再要求"
        "登录（凭据是模块票据，独立运行时该开关不生效）。"
        "前端用 BrowserRouter，挂到子路径下需要 basename，"
        "详见 docs/MODULES.md 的「SPA 模块」一节。"
    ),
)
