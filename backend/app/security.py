"""安全边界：Host 头校验 + 访问令牌 + 模块票据。

本地 Web 服务有两个经典风险，这里都要堵住：

1. **DNS rebinding** —— 攻击者把自己的域名解析到 127.0.0.1，
   诱导用户用该域名访问本机端口。浏览器会认为这是同源请求，
   于是本机的接口就被外部页面读写了。校验 Host 头即可防住：
   只有明确是回环地址的 Host 才放行。

2. **端口裸露** —— 只要绑的是 127.0.0.1，局域网就访问不到；
   再叠一层令牌，浏览器里误开的其他页面也无法直接调接口。

令牌走请求头，不放进 Cookie —— Cookie 会被同源的其它页面自动携带。

--------------------------------------------------------------- 浏览器会话

上面这条规则说的是**令牌**。但浏览器里还有另一件事要解决：令牌放在地址栏里
才能进门，页面一关、书签一存就废，自己天天用会极烦。

所以浏览器这条路径上多一层会话：令牌**只提交一次**，服务端换发一个
`HttpOnly` + `SameSite=Strict` 的会话 Cookie 回去，之后浏览器凭它进出。
Cookie 里装的是随机会话号，**不是令牌** —— 令牌泄露等于中台失守，
会话号泄露只能用在同一台机器上，而且能一键注销。细节见 `services/session.py`。

判定因此变成三条通道，**任一条成立都放行**：

| 通道 | 谁在用 | 凭什么 |
|---|---|---|
| 中台令牌 | 中台前端、脚本 | 请求头 `X-LocalDeck-Token` |
| 浏览器会话 | 用户自己的浏览器 | 会话 Cookie `pw_session` |
| 模块票据 | 模块页面里的子资源 | 查询参数 `_t` 或每模块自己的 Cookie |

一条都不成立时，日志里会写明**是哪个环节不对** —— 见 `_api_denial` /
`_module_denial`。这是本次改动的重点之一：以前只有一个笼统的 401，
现在日志里直接给答案。

--------------------------------------------------------------- 模块票据

模块页面挂在 `/opsgen/` 这样的子路径下，页面里的 CSS/JS/图片/表单
**带不上自定义请求头**。给它们单独开一条凭据通道是必须的，但**不能**
用中台令牌 —— 模块是第三方代码，中台令牌一旦落进它的页面 JavaScript，
它就能反过来调中台的 /api/*（读全部本机软件清单、改外观配置）。

所以引入**模块级票据**，与中台令牌是两回事：

| | 中台令牌 | 模块票据 |
|---|---|---|
| 存哪 | 只走请求头 `X-LocalDeck-Token` | 只对某个 mount 生效的 HttpOnly Cookie |
| 作用域 | 整个中台 | 仅 `/opsgen/*` |
| 谁拿得到 | 只有中台自己的前端 | 浏览器自动带，模块脚本读不到（HttpOnly） |
| 泄露后果 | 中台失守 | 只能访问那一个模块自己的路径 |

判定是「路径 + 方法」双重判断的，见 `needs_token()`。
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Callable, NamedTuple

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from . import config

log = logging.getLogger("localdeck.http")

_TOKEN_HEADER = "x-localdeck-token"
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
# 静态资源与首页本身不含任何业务数据，放行以免首次打开就卡住。
_OPEN_PREFIXES = ("/api/health",)
_OPEN_EXACT = {"/", "/index.html", "/style.css", "/app.js", "/favicon.ico", "/favicon.svg"}
# 免令牌的「读」接口：路径 → 允许的方法集合。
# 之所以按方法细分，是因为 /api/branding 的 GET 必须放开（页面首屏要用它配色），
# 但同一个路径的 PUT 会改配置，绝不能跟着一起放开。
#
# /api/session 是唯一整条免令牌的路径，因为它就是进门的那道门：
# POST 要能匿名提交令牌（否则换不到会话），DELETE 要能匿名清 Cookie
# （否则会话失效后连登出的机会都没有）。GET 只回一个布尔值，不泄露东西。
_OPEN_READS: dict[str, set[str]] = {
    "/api/branding": {"GET", "HEAD"},
    "/api/session": {"GET", "HEAD", "POST", "DELETE"},
}
# 访问日志跳过静态资源：一次页面加载就是四条同质的日志，会把有用的信息冲淡。
_QUIET_PATHS = frozenset({"/style.css", "/app.js", "/favicon.ico", "/favicon.svg"})

# 模块票据的查询参数名。用 `_t` 这么短的名字是为了让它出现在地址栏里
# 也不刺眼 —— 它本来就不是长期凭据，进门一次就换成 Cookie 了。
TICKET_PARAM = "_t"


class ModuleCredential(NamedTuple):
    """某个模块路径的凭据信息。

    用具名元组而不是裸元组：这里有三个都是字符串的字段，
    顺序写错不会报错，只会表现成「Cookie 被种到了奇怪的路径上」这类
    极难查的现象。
    """

    ticket: str = ""
    cookie_name: str = ""
    mount: str = ""
    # 这个模块是不是**中台自己的代码**（kind=native）。
    # 中间件据此决定要不要放行浏览器会话 —— 见下面 `_guard` 里的说明。
    native: bool = False


NO_MODULE = ModuleCredential()

# 判定「这个请求是不是某个模块的请求」的函数：路径 → 凭据信息。
# 返回 NO_MODULE 表示不是模块路径，此时模块通道关闭，只认中台令牌与会话。
ModuleTicketLookup = Callable[[str], ModuleCredential]


def host_allowed(host_header: str) -> bool:
    """只接受回环地址作为 Host，端口不参与判断（端口可被用户改）。"""
    if not host_header:
        return False
    name = host_header.strip().lower()
    if name.startswith("["):  # IPv6 字面量形如 [::1]:8731
        end = name.find("]")
        if end < 0:
            return False
        name = name[1:end]
    elif ":" in name:
        name = name.rsplit(":", 1)[0]
    return name in _LOOPBACK_NAMES


def token_of(request: Request, *, allow_query: bool = True) -> str:
    supplied = request.headers.get(_TOKEN_HEADER) or ""
    if not supplied and allow_query:
        supplied = request.query_params.get("token") or ""
    return supplied


def token_ok(request: Request, expected: str, *, allow_query: bool = True) -> bool:
    """校验中台令牌。

    `allow_query=False` 用于模块路径 —— 见下面 install_security 里的说明：
    在模块页面上，URL 查询串是第三方 JavaScript 读得到的地方，
    中台令牌绝不能被允许出现在那里。
    """
    supplied = token_of(request, allow_query=allow_query)
    if not supplied:
        return False
    return secrets.compare_digest(supplied, expected)


def needs_token(path: str, method: str = "GET") -> bool:
    if path in _OPEN_EXACT:
        return False
    if path.startswith(_OPEN_PREFIXES):
        return False
    allowed = _OPEN_READS.get(path.rstrip("/") or "/")
    if allowed and method.upper() in allowed:
        return False
    if path.startswith("/api"):
        return True
    # 其它一切（含模块路径与未来新增的静态路由）默认要求令牌，宁可严格。
    return True


def module_credential_ok(
    request: Request, expected_ticket: str, cookie_name: str
) -> bool:
    """模块路径的第二条凭据通道。

    只认两样东西，且都必须等于这个模块自己的票据：
    - 查询参数 `_t`（首次进入模块时用）
    - Cookie `pw_mod_<id>`（之后浏览器自动带）

    **刻意不认中台令牌的查询参数形式** —— 中台令牌只走请求头，
    这样它永远不会出现在任何 URL 里、也就落不进模块页面的
    `location.search`。
    """
    if not expected_ticket:
        return False
    supplied = request.query_params.get(TICKET_PARAM) or ""
    if not supplied:
        supplied = request.cookies.get(cookie_name) or ""
    if not supplied:
        return False
    return secrets.compare_digest(supplied, expected_ticket)


def _api_denial(request: Request) -> str:
    """把「为什么被拒」说成人话。

    这是本次改动里最实用的一个小东西：以前 401 只有一个笼统的 detail，
    客户端看不见原因、日志里也没留，出问题时只能靠猜。现在每种情况
    对应一句明确的判断，直接写进日志。
    """
    if request.headers.get(_TOKEN_HEADER):
        return "请求头里的令牌与当前令牌不符（换过数据目录？或删过 data/.token？）"
    if request.cookies.get(config.SESSION_COOKIE):
        return "会话已失效（过期 / 已登出 / 令牌被重置后集体作废）"
    if request.query_params.get("token"):
        return "只在查询串里给了令牌，但受保护接口不收查询串形式"
    return "没带任何凭据（请求头与会话 Cookie 都没有）"


def _module_denial(request: Request, cred: ModuleCredential) -> str:
    if not cred.ticket:
        return "这个模块没有可用票据（可能尚未就绪，或被停用）"
    if request.query_params.get(TICKET_PARAM) or request.cookies.get(cred.cookie_name):
        return f"模块票据与当前轮次不符（{cred.cookie_name} 是旧的，模块可能重启过）"
    if request.headers.get(_TOKEN_HEADER):
        return "请求头里的令牌与当前令牌不符"
    if not cred.native and request.cookies.get(config.SESSION_COOKIE):
        return "第三方模块路径不认浏览器会话，需要令牌或模块票据"
    return "模块路径上既没有票据，也没有中台令牌"


def _log_request(request: Request, path: str, status: int, started: float) -> None:
    """记一条访问日志。

    记的是**路径**而不是完整 URL，参数**只记名字不记值** —— 模块入口的
    `?_t=<票据>` 和首页的 `?token=<令牌>` 都躺在查询串里，整条 URL 落盘
    等于把凭据写进日志。这是 logging_setup.py 里第 2 条规矩的具体落实。
    """
    if path in _QUIET_PATHS:
        return
    elapsed = (time.perf_counter() - started) * 1000
    names = sorted(request.query_params.keys())
    suffix = f" ?{','.join(names)}" if names else ""
    write = log.info if status < 400 else log.warning
    write("%s %s -> %d  %.0fms%s", request.method, path, status, elapsed, suffix)


def install_security(
    app: FastAPI,
    token: str,
    ticket_lookup: ModuleTicketLookup | None = None,
    session_verify: Callable[[Request], bool] | None = None,
) -> None:
    @app.middleware("http")
    async def _guard(request: Request, call_next):
        started = time.perf_counter()
        path = request.url.path

        if not host_allowed(request.headers.get("host", "")):
            log.warning(
                "拒绝 %s %s：Host 头 %r 不是回环地址（疑似 DNS rebinding）",
                request.method,
                path,
                request.headers.get("host", ""),
            )
            return JSONResponse(
                {"detail": "Host 头不被允许：本服务只接受通过 127.0.0.1 访问"},
                status_code=400,
            )
        # 本次请求若用一次性票据进的模块，就在响应里把长期 Cookie 种下。
        pending_ticket_cookie: tuple[str, str, str] | None = None
        denial = ""

        if needs_token(path, request.method):
            cred = ticket_lookup(path) if ticket_lookup else NO_MODULE
            is_module_path = bool(cred.ticket)

            if is_module_path:
                # 模块路径的通道：
                #   1) 中台令牌 —— **只认请求头形式**
                #   2) 模块票据 —— 查询参数 `_t` 或本模块的 HttpOnly Cookie
                #   3) 浏览器会话 —— **只有 native 模块能走这条**，见下
                #
                # 第 1 条为什么砍掉查询参数形式：`?token=` 会留在地址栏和
                # location.search 里，而模块页面是第三方代码 —— 它一句
                # `new URLSearchParams(location.search).get('token')` 就能把
                # 中台令牌捞走，然后去调 /api/apps 读走本机全部软件清单。
                # 中台自己的前端压根不需要用 URL 传令牌（它走请求头），
                # 所以这里收紧不损失任何功能。
                #
                # 第 3 条为什么只给 native：native 模块是中台自己的代码
                # （modules/inventory 这些），它的页面和中台前端是同一批人写的，
                # 放行会话等于"自己的前端访问自己的接口"。而 subprocess_proxy
                # 模块装着**第三方代码**，会话一旦能被它消费，第三方页面里的
                # 任何脚本都能借浏览器之手把中台接口全调一遍 —— 那是拿不到令牌
                # 也照样能拿到本机软件清单、审计流水的路，必须堵死。
                by_ticket = module_credential_ok(request, cred.ticket, cred.cookie_name)
                by_header = token_ok(request, token, allow_query=False)
                by_session = bool(
                    cred.native and session_verify and session_verify(request)
                )
                allowed = by_ticket or by_header or by_session

                # 用 `_t` 进门 → 换成 Cookie。页面里的 CSS/JS/图片、表单 POST、
                # 前端拼出来的 XHR 都带不上自定义请求头，没有这个 Cookie
                # 它们会全部 401。
                #
                # 这件事**必须放在中间件里**，不能放在代理的响应改写里：
                # `native` / `static` 模块同进程挂载、根本不走代理，
                # 放代理里它们就永远拿不到 Cookie，页面第一个子资源就挂。
                if not allowed:
                    denial = _module_denial(request, cred)
                elif by_ticket and request.query_params.get(TICKET_PARAM):
                    pending_ticket_cookie = (cred.cookie_name, cred.ticket, cred.mount)
            else:
                # 中台自己的接口：令牌请求头 或 浏览器会话，有一条成立就放行。
                # 特意保留「请求头」这条路 —— 脚本、curl、模块之间的调用
                # 都没有 Cookie，它们凭令牌照样能用。
                allowed = token_ok(request, token) or bool(
                    session_verify and session_verify(request)
                )
                if not allowed:
                    denial = _api_denial(request)

            if not allowed:
                log.warning("401 %s %s —— %s", request.method, path, denial)
                return JSONResponse(
                    {
                        "detail": "缺少或错误的访问令牌（模块页面的凭据只在它自己的路径下有效）",
                        "path": path,
                    },
                    status_code=401,
                )

        response = await call_next(request)
        if pending_ticket_cookie:
            name, value, mount = pending_ticket_cookie
            response.headers.append(
                "set-cookie",
                f"{name}={value}; Path={mount}; HttpOnly; SameSite=Strict; Max-Age=86400",
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        _log_request(request, path, response.status_code, started)
        return response


def build_entry_url(token: str) -> str:
    return f"http://{config.HOST}:{config.PORT}/?token={token}"
