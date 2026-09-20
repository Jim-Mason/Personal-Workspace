# 模块接入规范

本文回答一件事：**把一个新的运维小工具接进中台，需要动什么、不能动什么。**

适用对象是任何第三方或自研的服务 —— Flask、FastAPI、Node、纯静态产物都能接。
前置阅读：[ARCHITECTURE.md](ARCHITECTURE.md) 解释了「为什么是单端口、为什么不上容器」。

---

## 一、五分钟接入

三步：

```
modules/
  mytool/                    ← 模块目录，名字就是 id
    module.py                ← 新增：唯一的声明文件（中台读它）
    requirements.txt         ← 模块自己的依赖（模块私有 venv 用）
    app.py engine/ static/ … ← 模块原有源码，原样不动
```

1. **建目录**，把模块的代码原样放进去。
2. **写 `module.py`**，只写一句 `MODULE = ModuleSpec(...)`。
3. **装依赖**：`scripts\setup-modules.bat --only mytool`，然后在中台「模块」标签页点「重载声明」→「启动」。
   （不带 `--only` 就是处理所有模块；加 `--force` 会把已建好的环境删掉重建。）

中台会用的启动命令等价于：

```
modules\mytool\.venv\Scripts\python.exe  <中台>\backend\app\services\modules\runners\flask_app.py
```

工作目录是模块目录，监听参数、调试开关、超时全部由中台给定。

---

## 二、`module.py` 怎么写

```python
MODULE = ModuleSpec(          # ModuleSpec 由中台在加载时注入，不需要 import
    id="mytool",              # 必须与目录名一致
    name="我的运维工具",        # 导航与卡片上的显示名
    description="一句话说清它干什么",
    version="1.0",
    order=30,                 # 排序，小的在前；留间隔方便插队

    kind="subprocess_proxy",  # 接入形态，见第三节

    # ---- subprocess_proxy 专用 ----
    runner="flask_app",       # 中台提供的启动壳（见第五节）
    app_module="app",         # 模块内要 import 的模块名
    app_object="app",         # 其中的 WSGI/ASGI 应用对象
    socketio_object="",       # 非空 → 走 socketio.run
    internal_port=8733,       # 必填，1024–65535，且只绑 127.0.0.1
    health_path="/",          # 就绪探测的路径
    startup_timeout=30.0,
    auto_start=True,

    # ---- 安全开关（默认都是关的，见第八节）----
    allow_realtime=False,
    allow_external_assets=False,

    env={"SECRET_KEY": "..."},   # 额外环境变量；不许写 host / bind / listen
    # python="",                 # 留空则找 modules/<id>/.venv
)
```

### 字段速查

| 字段 | 默认 | 说明 |
|---|---|---|
| `id` | — | 必须等于目录名。日志、数据目录、审计流水靠它对应 |
| `name` / `description` / `version` | — | 展示用 |
| `order` | `50` | 导航排序，小的在前 |
| `kind` | `subprocess_proxy` | 见第三节 |
| `mount` | `/<id>` | 对外路径。只允许一级路径段，不允许占用 `/api` 等中台保留路径 |
| `enabled` | `True` | 关掉后中台不为它提供代理，导航里显示为「已禁用」 |
| `runner` | — | `subprocess_proxy` 必填。中台提供的启动壳名 |
| `app_module` / `app_object` | `app` / `app` | 模块内要 import 的名字 |
| `socketio_object` | `""` | 非空则用 `socketio.run` 启动（Flask-SocketIO） |
| `internal_port` | — | 必填。中台启动前会探测占用，被占会明确报错并指出是哪个 PID |
| `health_path` | `/` | 探活路径。4xx/5xx 也算「已就绪」（说明端口已在监听） |
| `startup_timeout` | `25.0` | 探活超时。超时会把半死的子进程收掉，不留孤儿 |
| `auto_start` | `True` | 中台起来后自动拉起；关掉就手动在面板里点启动 |
| `allow_realtime` | `False` | 是否放行 socket.io / WebSocket 通道 |
| `allow_external_assets` | `False` | 是否允许页面加载外部 CDN 资源 |
| `env` | `{}` | 额外环境变量。`host`/`bind`/`listen` 会被拒绝 |
| `python` | `""` | 指定解释器；一般留空，用 `modules/<id>/.venv` |
| `requirements` | `requirements.txt` | 私有依赖清单，供 `setup-modules.bat` 用 |
| `router_object` | `""` | `native` 形态：模块里 `APIRouter` 的对象名 |
| `static_dir` | `static` | `static` 形态：相对模块目录的产物目录 |
| `source_url` / `license` | `""` | 合规信息，会显示在卡片上。MIT/Apache 类许可要求保留出处 |
| `note` | `""` | 给未来的自己留一句话 |

用纯 `dict` 声明也可以（`MODULE = {…}`），字段名一一对应。
写了不认识的字段会**直接报错**而不是静默忽略 —— 写错的字段被忽略是最难查的一类问题。

---

## 三、四种接入形态怎么选

| kind | 中台做什么 | 模块要改造吗 | 什么时候用 |
|---|---|---|---|
| `subprocess_proxy` | 拉起子进程 + 探活 + 反代 + 回收 | **不用**，原样跑 | **默认选它。** Flask / Node / 任何能监听端口的东西 |
| `native` | `include_router` 挂进中台进程 | 要，得提供 FastAPI `APIRouter` | 自研的、愿意跟中台同生共死的模块 |
| `static` | 托管前端构建产物 | 不用，给产物就行 | 纯前端页面、单页应用 |
| `external` | 只反代，不管进程 | 不用 | 模块由 systemd / 另一个窗口自己启动 |

**优先 `subprocess_proxy`。** 理由很实际：第三方项目随时要 `git pull` 同步上游，
任何「需要改造才能接入」的方案都会让这件事变成手工合并 —— 迟早会停下来不做。

### 为什么 Flask 不能塞进中台进程

中台跑在 ASGI 上，Flask 是 WSGI。技术上能用 `WSGIMiddleware` 挂进去，但会丢两样：

1. **WebSocket 没了** —— WSGI 协议不支持，依赖实时通道的功能直接失效
2. **两套中间件与生命周期语义错位** —— 各自的异常处理、启动钩子混在一起，出问题极难定位

所以走「子进程 + 反代」。

---

## 四、内部端口与对外路径

```
浏览器  ──►  http://127.0.0.1:8731/opsgen/template/nginx
                       │
                       │  中台：鉴权 → 剥/补请求头 → 反代 → 改写返回体
                       ▼
               http://127.0.0.1:8732/template/nginx
```

**内部端口只绑 `127.0.0.1`，永远不会出现在用户看到的地址里。** 端口分配约定：

| 端口 | 归谁 |
|---|---|
| 8731 | 中台本体 |
| 8732 | 模块二 OpsGen |
| 8733+ | 后续模块依次占位 |

新增模块时先查一下 `netstat -ano -p TCP | findstr 8733`，别撞车。
即使撞了中台也会在启动前发现并明确报错：

> 内部端口 8732 已被占用（PID 29764 / python.exe），且健康检查 / 无响应（TimeoutError）。

如果端口被占**但健康检查能通过**，中台会当作「上次残留的进程」直接接管，
并在卡片上标注「接管自外部进程」—— 这种情况下中台退出时不会去杀它。

---

## 五、启动壳：为什么模块不能自己决定监听地址

上游代码里 `host="0.0.0.0"` 是极常见的默认值。它把内部端口暴露到所有网卡上，
局域网里任何人都能直连，**绕开中台的全部鉴权、审计与路径改写**。
`debug=True` 更麻烦 —— Werkzeug 调试器自带交互式控制台，本身就是一条现成的命令执行链。

但「不改第三方源码」同样是硬约定（否则没法跟上游同步）。两者怎么同时满足？

**答案是：根本不执行模块的 `__main__` 分支。**

`import app` 只会触发模块级代码（建 Flask 对象、注册路由），
不会跑到 `if __name__ == "__main__":` 下面去。中台用自己的壳去拿它的 `app` 对象，
再由壳决定怎么起服务：

```python
# backend/app/services/modules/runners/flask_app.py
HOST = "127.0.0.1"        # 写死。环境变量和命令行都改不动它
socketio.run(app, host=HOST, port=port, debug=False, use_reloader=False, ...)
```

可验证的结果：上游 `app.py` 里 `host="0.0.0.0"` 原封不动，
但 `netstat` 看到的是 `TCP 127.0.0.1:8732 LISTENING`。
这一条是自检脚本里的正式断言项（`backend/scripts/check_modules.py`）。

需要新的框架壳时，在 `backend/app/services/modules/runners/` 下加一个文件，
在 `module.py` 里用 `runner="文件名"` 指过去。壳里**不许**接受监听地址参数。
（现有两个：`flask_app.py` 给 Flask / Flask-SocketIO，`asgi_app.py` 给 FastAPI / Starlette。）

### 同进程挂载的模块没有壳

`kind=native` 的模块**不起进程、不开端口**，所以既不需要启动壳，也没有监听地址
可谈 —— 中台直接 `include_router(模块的 APIRouter, prefix=挂载点)`。
少一个端口就少一处要防的地方，所以「能用同进程就用同进程」是默认取向：
没有 WebSocket、没有阻塞式长任务、不自带依赖集的模块，都该走 native。

代价是它与中台同处一个进程，代码写崩会连带中台一起崩；也确实拿得到中台的内存
（所以模块里**不要**去 import 中台的包，见下节）。这个取舍在「同一台机器、同一个人用」
的前提下是划算的。

---

## 六、native 模块的三条约定

### 1. 导入：每个模块是独立的包

中台把 `modules/` 当作一个命名空间包的根，每个模块以 `localdeck_modules.<id>`
的身份被导入：

```python
importlib.import_module(f"localdeck_modules.{module_id}.{app_module}")
```

这么做是为了**避免顶层名字撞车**。早先的写法是把模块目录塞进 `sys.path` 再
`import app_module` —— 那样一个模块里的 `store.py` 会成为全局的顶层 `store`，
两个模块各有一个同名文件时后导入的会把先导入的顶掉，而现象是「调用到的函数
不是我以为的那个」，排查起来毫无线索。

对应地，**模块内部请用相对导入**（`from . import store`）。这样照着「一个包该长
什么样」写就行，不必去猜中台希望怎么摆。

### 2. 上下文：走环境变量，不要 import 中台

模块要能**脱离中台源码单独存在**。所以中台在导入 native 模块**之前**，把只读的
路径信息经环境变量交过去（`hub._export_native_context()`）：

| 环境变量 | 指向 | 谁该用 |
|---|---|---|
| `LOCALDECK_MODULE_DATA_DIR` | **本模块目录下的 `data/`** | 大多数模块：自己的配置放自己家里 |
| `LOCALDECK_DATA_DIR` | 中台的 `data/` | 只有需要写平台级表（`resource`/`task`/`event`）的模块 |
| `LOCALDECK_DB_PATH` | 中台的 `localdeck.db` | 同上 |
| `LOCALDECK_MACHINE_ID` | 机器标识 | 写 `task` 时要带 |
| `LOCALDECK_MOUNT` | 本模块挂载点 | 自带页面的模块拼自己的资源与接口地址 |

模块侧的通行写法是 `context.py`：读得到就用，读不到就退回自己目录下的 `data/`
—— 那条路正好是独立调试用的。参看 `modules/inventory/context.py`、
`modules/filelist/context.py` 与 `modules/logviz/context.py`。

⚠️ **顺序要紧**：中台必须在**导入之前**把环境变量设好。模块的 `context.py` 是在
import 期读环境的，顺序反了它就会静默连到另一个库上。

### 3. 入口页：每个模块都该有一个

中台的「模块」面板给每个模块都提供「打开」，点开就是它挂载点的根路径。
**没有入口页的话那是个 404**，用户会以为模块坏了。

自带界面的模块（如 `modules/filelist/`、`modules/logviz/`）拿入口页当首页即可；
界面长在中台外壳里的模块（如 `modules/inventory/`）也给一个说明页，
把「界面在哪」讲清楚并给个跳转，好过一个死链接。

顺带一条：`native` / `static` 模块里**没有任何路由匹配**的路径，最后会落到中台的
代理兜底路由。那里会明确返回 404 并说明「这是同进程挂载的模块，没有这个路径」，
**不会**去尝试"转发" —— 它们根本没有内部端口，转发只会得到一句把人带偏的
「连不上模块进程」。

---

## 七、挂在中台后面时免登录（可选）

一个自带登录体系的模块挂到中台后面时，用户已经过了一道门（中台的访问令牌），
再让他敲一遍模块自己的密码就是重复劳动。中台支持把这件事关掉，做法是：

1. 模块声明里注入 `env={"TRUST_LOCAL_PROXY": "1"}`（开关由**中台**给，模块自己不设默认）
2. 模块侧收到的每个代理请求都带 `X-LocalDeck-Module-Ticket` 头，
   值是**本模块自己的票据**（每次运行新生成，只在中台与模块之间流动）
3. 模块侧的判定要**三条同时成立**：开关打开 + 来源是回环 + 票据一致

**只认回环或只认 `X-Forwarded-Prefix` 是不够的** —— 本机任何进程都能伪造那些头。
票据是真凭据。参考实现：`modules/portal/backend/app/api/deps.py` 的
`local_trust_active()`。

独立运行时（不经中台启动）环境变量不存在，票据无从比对，只能正常登录 ——
这正是想要的默认：**免登录的正当性完全来自上游那道边界，边界不在就不能免。**

接口、网关、**以及 WebSocket** 都要走同一个判定函数。少改一处，现象是
「页面能打开但实时部分永远不更新」，极难归因。

---

## 八、两个默认关闭的安全开关

### `allow_realtime` —— 实时通道

很多运维工具的实时通道上只挂着一个功能：**在服务器上执行脚本**。
那等于给浏览器开了一个任意命令执行入口，中台的安全边界里明确写着「不提供」。

默认关闭后会发生什么：中台对 `/opsgen/socket.io/*` 返回 **403**，
同时在模块页面注入一个会说人话的替身，用户点「在线执行」时会看到：

> 中台未放行本模块的实时通道，「在线执行脚本」已停用。
> 原因：这一能力的实质是在本机执行任意脚本，等同于一个任何人都能通过浏览器触发的远程命令执行入口。
> ……脚本本身已经生成好了 —— 请复制到目标服务器上执行。

**不是静默失败**，用户知道发生了什么、也知道该怎么做。

要开的话，除了把 `allow_realtime` 改成 `True`，还必须自己补上：
白名单（哪些脚本能跑）+ 逐次确认 + 参数模板。这三样缺一不可。

### `allow_external_assets` —— 外部 CDN

对应「零网络上传、无遥测」。默认会把页面里的外部 `<script src>` / `<link href>` 剥掉，
换成一条 HTML 注释说明动了什么，并在模块日志里只提示一次。

代价通常是装饰性的（代码高亮、字体、图标），功能不受影响。
要保留就改成 `True`，代价是页面打开时会向第三方域名发请求。

---

## 九、凭据与鉴权（重要，接入新模块前必读）

### 三条独立的凭据通道

模块页面里的 CSS / JS / 图片 / 表单 **带不上自定义请求头**，所以必须有一条
「浏览器自动携带」的通道。但这条通道**不能**用中台令牌：

> 模块是第三方代码。中台令牌一旦落进它的页面 JavaScript
> （`new URLSearchParams(location.search).get('token')` 或翻 `sessionStorage`），
> 它就能反过来调中台的 `/api/*` —— 读走本机全部软件清单、改外观配置。

所以用的是**模块级票据**。而浏览器那边还有第三条通道：会话。

| | 中台令牌 | 浏览器会话 | 模块票据 |
|---|---|---|---|
| 存放 | 只走请求头 `X-LocalDeck-Token` | `HttpOnly` Cookie，`Path=/` | `HttpOnly` Cookie，`Path=/opsgen` |
| 谁在用 | 脚本、curl、模块之间的调用 | 用户自己的浏览器 | 模块页面里的子资源 |
| 作用域 | 整个中台 | 中台接口 + **native 模块** | 仅这一个模块的路径 |
| URL 里会出现吗 | **不会**（模块路径下连 `?token=` 都拒收） | 不会 | 只在进门那一次用 `?_t=`，随后换 Cookie |
| 泄露后果 | 中台失守 | 能调中台接口，但读不到值，且可一键注销 | 只能访问那一个模块自己的路径 |

**第三方模块路径不认会话**，这一条是刻意的：会话 Cookie 会被同源请求自动带上，
而模块页面是第三方代码 —— 放行等于把中台接口借给它用。
只有 native 模块（中台自己写的那些）才认会话，因为那批页面跟中台前端是同一批人写的。

进模块的完整流程：

```
1. GET /api/modules                     拿到 open_url（带模块票据）
2. GET /opsgen/?_t=<票据>                进门；响应种下 pw_mod_opsgen（HttpOnly; Path=/opsgen）
3. GET /opsgen/static/css/style.css     浏览器自动带 Cookie → 放行
4. 页面里 fetch('/api/templates')        垫片补前缀 → /opsgen/api/templates → 带 Cookie → 放行
```

票据每次中台重启就换一个，且不落盘。

### 已知的残余风险（留给下一阶段）

反代方案能把「凭据」隔开，但隔不开**同一个来源**：模块的 iframe 与中台首页
同属 `http://127.0.0.1:8731`，同源就意味着模块的脚本理论上能碰到父页面的东西。

这条风险在**浏览器会话上线之后变轻了**：中台令牌已经不再存进浏览器
（以前它在 `sessionStorage` 里，而同源的模块脚本可以直接读走、带在身上去任何地方用），
换成了 `HttpOnly` 的会话 Cookie —— 值读不到，但**借浏览器之手发同源请求**仍然可以，
因为 Cookie 是浏览器自己带的。所以规则是：

- 第三方模块路径**一律不认会话**，只认令牌与模块票据
- native 模块（中台自己写的：inventory / filelist / logviz）才认会话
- 至于「同源页面能不能调 `/api/*`」这件事，在同源之下没有协议层的解法

这在「单一入口」的前提下是天然存在的，不是本次实现的疏漏。
处理它的方向是**来源隔离** —— 让模块内容从另一个回环端口提供，
使 iframe 成为真正的跨源，浏览器自己就会把两边隔开。
这属于「统一导航与鉴权」那一阶段要决策的事，见 [ARCHITECTURE.md](ARCHITECTURE.md) 第四节末尾。

---

## 十、排障

### 模块卡片显示「启动失败」

点卡片上的「日志」，看尾部。最常见两种：

| 日志里看到 | 原因 | 怎么办 |
|---|---|---|
| `ModuleNotFoundError: No module named 'flask'` | 依赖装错地方了 | `scripts\setup-modules.bat --only <id> --force` |
| `端口 873x 已被占用（PID …）` | 上次残留的进程，或端口撞车 | 按 PID 结束它，或改 `internal_port` |

日志里会明确写「解释器：…」那一行 —— 依赖问题基本看这一行就能定位。

### 页面能打开，但按钮点了没反应

看模块日志里有没有对应的请求。没有的话是**前端的路径没被改写**：

1. 页面的链接是不是用 `<a href>` / `<form action>` 之外的奇技淫巧生成的？
   （`window.open()`、`document.location` 拼接字符串等）
2. 第三方库动态构造的路径会被 `assets/pathfix.js` 兜住：
   它给 `fetch` / `XMLHttpRequest.open` / `location.assign` 打了补丁。
   有新的动态 API 就把补丁补进去。
3. `/api/...` 这类根路径的请求，日志里应该看到 `/api/...`（模块自己的根路径），
   **不是** `/opsgen/api/...` —— 中台转发时会剥掉前缀。

### 页面样式丢了 / 图片裂了

多半是新出现的外部资源被剥离了。日志里搜「已剥离页面里的外部 CDN 资源」。
要么把资源本地化，要么临时把 `allow_external_assets` 打开。

### 到底改写了什么

`backend/app/services/modules/rewrite.py` 只做四件事，都是白名单式的：

| 载体 | 规则 |
|---|---|
| HTML 属性 | `href` / `src` / `action` / `poster` / `formaction` / `data-url` / `data-href` / `data-action` |
| CSS | `url(...)` |
| 运行时跳转 | `location.href = "/x"`、`location.assign("/x")`、`location.replace("/x")` |
| 响应头 | `Location` 补前缀；`Set-Cookie` 的 `Path` 收进模块路径 |

**刻意不改 JSON 响应体**：接口返回的可能是生成好的脚本内容，里面出现 `/etc/nginx`
这种字符串太正常了，无差别改写会把用户要的产物弄脏。
运行时那部分由注入的 `pathfix.js` 在前端补，够用。

---

## 十一、自检

改完内核或接入新模块后跑一遍：

```
backend\scripts\check_modules.py --module inventory   # 模块一（12 项）
backend\scripts\check_modules.py --module filelist    # 模块四（18 项）
backend\scripts\check_modules.py --module opsgen      # 模块二（34 项，也是默认值）
backend\scripts\check_modules.py --module logviz      # 模块五（20 项）
backend\scripts\check_modules.py --module portal      # 模块三（23 项）
backend\scripts\check_modules.py --template nginx     # 换业务路径检查用的模板
```

模块五另外有一个**解析器级**的自检，不连服务、随时能跑（66 项）：

```
modules\logviz\selftest_parsers.py                    # 三个解析器的排版识别与归并（66 项）
```

两个数不一样，别混：`--module logviz` 的 20 项查的是**模块内核**（页面托管、
资源逐字节、越界拒绝、源码白名单），`selftest_parsers.py` 的 66 项查的是
**解析质量**（三种日志的排版变体、异常栈合并、指纹归一、TopN 排序）。前者要中台
在跑，后者不需要。

第 1~3 节与第 11 节是**与模块无关**的内核判据，任何模块都要过；
第 4~10 节按模块给具体用例（opsgen / portal / filelist / logviz 各有一套，其它模块记为跳过）。

**第一节到第三节：凭据边界。** 裸访问 401、令牌不许走查询串、令牌走请求头可以进、
**从入口地址进入时下发票据 Cookie**（HttpOnly + Path 收拢到模块路径）、票据能进模块。

> 这三节对 `native` / `static` 模块走**不同的第一项判据**：它们没有自己的进程，
> `state` 恒为 `stopped`，拿它判「跑没跑」会永远失败。这类模块的等价判据是
> `proxy_ready`（＝已挂载）。同一件事在脚本里写作「模块 xx 已挂载（同进程挂载）」。

**第 4~9 节：内核行为（opsgen 用例）。** 路径改写（前缀已补 / 无残留裸路径 / 垫片已注入）、
外部资源剥离、子资源与 JSON 接口、实时通道 403、路径穿越拒绝、
**票据不能访问中台 `/api/*`**、以及**上游源码未被改动**（断言 `host="0.0.0.0"` 还在原处）。

**第 10 节：真实用户路径（opsgen 用例）。** 表单 `action` 是否补了前缀、
POST 请求体能否转发、一键生成的重定向 `Location`、
结果页里拼出的分享链接是否换成了中台地址、页面是否泄露内部端口，
以及三条关于**下载**的断言 —— 其中最硬的一条是：

```
[OK] 代理字节 == 模块直出字节（逐字节相同）  —— 1149 字节
```

它拿「经中台代理下载到的字节」和「绕过中台直连模块端口拿到的字节」逐个比对。
改写层该动的只有 HTML 和 CSS；一旦它伸手改了生成的脚本正文，
用户下载到的就是个坏文件，而且往往要到执行时才炸 —— 这条断言把这种情况钉死。

**第 11 节：会话隔离（与模块无关，务必别删）。** 见下一节。

（`--module portal` 时第 4~10 节换成模块三的用例：SPA 首页资源前缀、垫片注入、
入口 JS 可加载、**构建产物会读 `__LOCALDECK__.mount`**（basename 生效）、
代理字节与直出字节一致、SPA 深链接回落首页、
模块自带网关注入的基路径带前缀、**内网页面经两层代理可取回**。）

（`--module filelist` 时换成模块四的用例：页面里的挂载点占位符已替换、
引用的资源带前缀且**字节等于磁盘原文**、书库列表与列目录接口可用、
以及**6 个越界探针必须全部被拒** —— `..`、多级回溯、盘符绝对路径、
POSIX 绝对路径、反斜杠写法。最后这条是这个模块唯一的安全阀，最要紧。）

（`--module logviz` 时换成模块五的用例：占位符与资源前缀、资源字节对照、
**源码不在资源白名单里**（`/assets/module.py` 必须取不到 —— 模块源码就在 `web/` 的上一级）、
日志目录与列目录接口可用、类型探测接口给出 `scores`/`best`/`detected`，
以及**9 个越界探针必须全部被拒**。模块五比模块四更需要证明这条：
模块四越界只是把目录列出来，模块五越界是**把任意文件的内容读出来给人看**。
探针不止查 `/api/list`，`/api/detect`、`/api/analyze`、`/api/preview` 各单独探一遍 ——
它们是不同入口，"只测其中一个"就会漏掉另一个写成裸奔的情况。）

---

## 十二、会话隔离：一条被实测抓出来的越权

这一节单独写，因为它是这个内核里**最容易再犯、也最难用眼睛发现**的一类错。

### 症状

零 Cookie（只带中台票据）访问 `/portal/api/auth/me`，返回 **200 + 管理员身份**。
`/api/cards`、`/api/users` 同样能读。

### 根因

中台代理用的是**一个长期存活、被所有模块共用**的 HTTP 客户端（`httpx.AsyncClient`），
而 httpx 默认会给每个客户端配一个 Cookie 罐，并在**每个响应到达时**执行
`self.cookies.extract_cookies(response)`；之后的出站请求再由 `_merge_cookies()`
把罐里的 Cookie 合并进去。于是这个客户端变成了一个有状态的东西：

1. 用户在 `/portal/` 登录一次 → 模块返回的 `dtb_access` 被存进罐里 →
   此后**任何**没有带 Cookie 的请求被转发时，罐里的会话都会被自动贴上。
2. Cookie 的作用域只看域名、**不看端口**。中台所有模块都在 `127.0.0.1` 上
   （8732 / 8733 只是端口不同），所以 A 模块的会话会被原样送给 B 模块 ——
   「模块是第三方代码、凭据不互通」这条隔离直接失效。

### 修法

`backend/app/services/modules/proxy.py` 里两处：

- `_disable_cookie_jar(client)` —— 在**罐实例**上把 `extract_cookies` / `set_cookie`
  置空，写入通路就断了。`Client.cookies` 的 setter 会把赋进去的对象重新包成普通
  `Cookies`，所以走公开属性改不掉，只能在实例上动手。
- `_forward()` 里一道**不变量校验** —— 出站请求能带的 Cookie **名字集合**必须恰好等于
  本次请求该带的那一份，多一个就拒发这次转发。这是兜底：将来 HTTP 客户端换了行为，
  会立刻以「请求失败」的形式暴露，而不是安静地泄露会话。

### 与之配套的两条判据（`check_modules.py` 第 11 节）

```
[OK] 代理客户端已关闭 Cookie 罐（不记忆模块会话）  —— 罐为空，出站不会带残留会话
[OK] 零 Cookie 重放的前置：模块确实发出过会话  —— 登录 HTTP 200（期望 200）
[OK] 零 Cookie 重放不应通过鉴权  —— /api/auth/me → HTTP 401（期望 401）
```

注意中间那一条 **"前置"**：如果登录本身没成功，后面的 401 就什么都证明不了。
先确认「模块确实发过会话」，再断言「零 Cookie 拿不到数据」，两条一起才有意义 ——
排查时踩过这个坑：登录请求漏带票据被中台挡在门外，于是第二步的 401 是个假证据。

失败项会打印明细并以退出码 1 结束，可以直接挂到 CI 或提交前钩子上。
