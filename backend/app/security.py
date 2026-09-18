"""安全边界：Host 头校验 + 访问令牌 + 模块票据。

本地 Web 服务有两个经典风险，这里都要堵住：

1. **DNS rebinding** —— 攻击者把自己的域名解析到 127.0.0.1，
   诱导用户用该域名访问本机端口。浏览器会认为这是同源请求，
   于是本机的接口就被外部页面读写了。校验 Host 头即可防住：
   只有明确是回环地址的 Host 才放行。

2. **端口裸露** —— 只要绑的是 127.0.0.1，局域网就访问不到；
   再叠一层令牌，浏览器里误开的其他页面也无法直接调接口。

令牌走请求头，不放进 Cookie —— Cookie 会被同源的其它页面自动携带。

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

import secrets
from typing import Callable

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from . import config

_TOKEN_HEADER = "x-localdeck-token"
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
# 静态资源与首页本身不含任何业务数据，放行以免首次打开就卡住。
_OPEN_PREFIXES = ("/api/health",)
_OPEN_EXACT = {"/", "/index.html", "/style.css", "/app.js", "/favicon.ico", "/favicon.svg"}
# 免令牌的「读」接口：路径 → 允许的方法集合。
# 之所以按方法细分，是因为 /api/branding 的 GET 必须放开（页面首屏要用它配色），
# 但同一个路径的 PUT 会改配置，绝不能跟着一起放开。
_OPEN_READS: dict[str, set[str]] = {"/api/branding": {"GET", "HEAD"}}

# 模块票据的查询参数名。用 `_t` 这么短的名字是为了让它出现在地址栏里
# 也不刺眼 —— 它本来就不是长期凭据，进门一次就换成 Cookie 了。
TICKET_PARAM = "_t"

# 判定「这个请求是不是某个模块的请求」的函数：路径 → (票据, Cookie 名, 挂载点)。
# 都不是模块路径时返回 ("", "", "")，此时模块通道关闭，只认中台令牌。
# 之所以要带上挂载点：换 Cookie 时得知道 `Path=` 该写什么，而挂载点只有
# 模块注册表知道。
ModuleTicketLookup = Callable[[str], tuple[str, str, str]]


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


def install_security(
    app: FastAPI, token: str, ticket_lookup: ModuleTicketLookup | None = None
) -> None:
    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if not host_allowed(request.headers.get("host", "")):
            return JSONResponse(
                {"detail": "Host 头不被允许：本服务只接受通过 127.0.0.1 访问"},
                status_code=400,
            )
        # 本次请求若用一次性票据进的模块，就在响应里把长期 Cookie 种下。
        pending_ticket_cookie: tuple[str, str, str] | None = None

        if needs_token(request.url.path, request.method):
            ticket, cookie_name, mount = (
                ticket_lookup(request.url.path) if ticket_lookup else ("", "", "")
            )
            is_module_path = bool(ticket)

            if is_module_path:
                # 模块路径两条通道：
                #   1) 中台令牌 —— **只认请求头形式**
                #   2) 模块票据 —— 查询参数 `_t` 或本模块的 HttpOnly Cookie
                #
                # 第 1 条为什么砍掉查询参数形式：`?token=` 会留在地址栏和
                # location.search 里，而模块页面是第三方代码 —— 它一句
                # `new URLSearchParams(location.search).get('token')` 就能把
                # 中台令牌捞走，然后去调 /api/apps 读走本机全部软件清单。
                # 中台自己的前端压根不需要用 URL 传令牌（它走请求头），
                # 所以这里收紧不损失任何功能。
                by_ticket = module_credential_ok(request, ticket, cookie_name)
                allowed = token_ok(request, token, allow_query=False) or by_ticket

                # 用 `_t` 进门 → 换成 Cookie。页面里的 CSS/JS/图片、表单 POST、
                # 前端拼出来的 XHR 都带不上自定义请求头，没有这个 Cookie
                # 它们会全部 401。
                #
                # 这件事**必须放在中间件里**，不能放在代理的响应改写里：
                # `native` / `static` 模块同进程挂载、根本不走代理，
                # 放代理里它们就永远拿不到 Cookie，页面第一个子资源就挂。
                if by_ticket and request.query_params.get(TICKET_PARAM):
                    pending_ticket_cookie = (cookie_name, ticket, mount)
            else:
                allowed = token_ok(request, token)

            if not allowed:
                return JSONResponse(
                    {
                        "detail": "缺少或错误的访问令牌（模块页面的凭据只在它自己的路径下有效）",
                        "path": request.url.path,
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
        return response


def build_entry_url(token: str) -> str:
    return f"http://{config.HOST}:{config.PORT}/?token={token}"
