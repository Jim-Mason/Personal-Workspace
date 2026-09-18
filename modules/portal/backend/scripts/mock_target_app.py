"""模拟"内网目标系统"，用于端到端验证反向代理。

刻意覆盖了容易踩坑的几类响应：
- 根路径绝对地址的 CSS/JS/图片（验证 HTML 重写）
- **与目标同源的绝对地址**（`http://127.0.0.1:9099/x`）与协议相对地址（`//host/x`）
  —— 对应真实场景里 Jenkins 用 `Server.getRootUrl()` 拼出的主题 CSS：
  客户端在跳板机场景下访问不到该 host，必须收进网关前缀，否则样式直接丢失。
- **JS 运行时拼出来的根路径接口**（验证客户端 shim + 服务端 Referer 兜底转发）
- GBK 编码页面（验证按字节重写不会乱码）
- gzip 压缩的 HTML（验证解压后重写）
- Set-Cookie 带 Domain/Secure/SameSite=None（验证 Cookie 修正）
- 站内/站外重定向（验证 Location 重写）
- 大文件流式响应（验证不做内存缓冲）
- WebSocket 回显（验证 WS 透传）
"""

from __future__ import annotations

import gzip

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

app = FastAPI(title="Mock Internal Target")

# `__ABS__` / `__PROTO__` 会在运行时被替换成"目标自己的绝对地址"，
# 这样换端口跑也依然成立（不写死 9099）。
PAGE_INDEX = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<title>内网示例系统</title>
<link rel="stylesheet" href="/static/app.css">
<link rel="stylesheet" href="__ABS__static/theme-dark/theme.css">
<script src="/static/app.js" defer></script>
</head><body>
<h1>内部系统首页</h1>
<img src="/img/logo.png" alt="logo">
<div style="background-image:url(/img/bg.png)">背景</div>
<img src="__PROTO__img/abs-logo.png" alt="协议相对">
<img src="__ABS__img/abs-logo.png" alt="绝对地址">
<a href="/page2">第二页</a>
<a href="__ABS__page2">绝对地址第二页</a>
<a href="__PROTO__page2">协议相对第二页</a>
<a href="//cdn.example.com/lib.js">CDN 资源</a>
<a href="https://example.com/external">外部链接</a>
<form action="/submit" method="post">
  <input name="name" value="abc">
  <button type="submit">提交</button>
</form>
</body></html>"""

PAGE_INDEX_GBK = """<!DOCTYPE html>
<html><head><meta charset="gbk"><title>GBK 页面</title>
<link rel="stylesheet" href="/static/app.css"></head>
<body><p>中文内容：设备迁移与字典录入</p></body></html>"""

APP_CSS = """.logo { background: url(/img/logo.png) no-repeat; }
.dark { background: url(__ABS__img/bg.png); }
body { color: #333; }"""

# 一个只通过"绝对地址"被引用的 CSS：验证 rewrite_css 也认得同源绝对地址，
# 且不会把真正的外链（example.com）一起改掉。
THEME_CSS = """.theme { background: url(__ABS__img/bg.png); color: #111; }
.theme .ext { background: url(https://example.com/bg.png); }"""

APP_JS = """window.MOCK_APP = true;
fetch('/api/data').then(r => r.json());
fetch('/api/v1/game-config').then(r => r.json());"""


def _abs_base(request: Request) -> str:
    """目标服务自己的绝对地址前缀，如 `http://127.0.0.1:9099/`。"""
    return str(request.base_url)


def _proto_prefix(request: Request) -> str:
    """协议相对前缀，如 `//127.0.0.1:9099/`。"""
    return f"//{request.url.netloc}/"


def _render(template: str, request: Request) -> str:
    return template.replace("__ABS__", _abs_base(request)).replace(
        "__PROTO__", _proto_prefix(request)
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    return Response(
        content=_render(PAGE_INDEX, request).encode("utf-8"),
        media_type="text/html; charset=utf-8",
    )


@app.get("/gbk")
async def gbk_page() -> Response:
    # 用 GBK 编码返回，且 content-type 不声明 charset
    return Response(content=PAGE_INDEX_GBK.encode("gbk"), media_type="text/html")


@app.get("/index.html")
async def index_html(request: Request) -> Response:
    """整页 HTML 挂在**非根、且不带尾斜杠**的路径上。

    对应真实场景：Jenkins 的入口是 `…:8081/login`（它的站点根 `/` 反而返回 403）。
    这类目标属于"页面作用域" —— 入口原样打开该页面，
    页面里的 `/static/**` 一律按**站点绝对路径**解析，
    而不是被拼成 `/login/static/**`（否则整页全是 404）。
    """
    return Response(
        content=_render(PAGE_INDEX, request).encode("utf-8"),
        media_type="text/html; charset=utf-8",
    )


@app.get("/gzip")
async def gzip_page(request: Request) -> Response:
    body = gzip.compress(_render(PAGE_INDEX, request).encode("utf-8"))
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={"Content-Encoding": "gzip"},
    )


@app.get("/static/app.css")
async def css(request: Request) -> Response:
    return Response(content=_render(APP_CSS, request), media_type="text/css")


@app.get("/static/theme-dark/theme.css")
async def theme_css(request: Request) -> Response:
    """模拟 Jenkins 的 `/theme-dark/theme.css`：只被绝对地址引用。"""
    return Response(content=_render(THEME_CSS, request), media_type="text/css")


@app.get("/static/app.js")
async def js() -> Response:
    return Response(content=APP_JS, media_type="application/javascript")


@app.get("/img/logo.png")
async def logo() -> Response:
    return Response(content=b"\x89PNG\r\n\x1a\nFAKE", media_type="image/png")


@app.get("/img/abs-logo.png")
async def abs_logo() -> Response:
    return Response(content=b"\x89PNG\r\n\x1a\nFAKEABS", media_type="image/png")


@app.get("/img/bg.png")
async def bg() -> Response:
    return Response(content=b"\x89PNG\r\n\x1a\nFAKEBG", media_type="image/png")


@app.get("/page2")
async def page2() -> Response:
    return Response(
        content="<html><head><title>第二页</title></head><body><a href='/'>返回</a></body></html>",
        media_type="text/html; charset=utf-8",
    )


@app.post("/submit")
async def submit(name: str = Form("")) -> JSONResponse:
    return JSONResponse({"received_name": name, "ok": True})


@app.get("/api/data")
async def api_data() -> JSONResponse:
    return JSONResponse({"items": [1, 2, 3], "note": "不应被重写"})


@app.get("/api/v1/game-config")
async def game_config() -> JSONResponse:
    """内网系统用**根路径**请求的接口。

    对应真实场景（闯关游戏页面 `fetch('/api/v1/game-config')`）：
    门户自己也有 `/api/*` 命名空间，所以这种请求绝不能落到门户路由上。
    """
    return JSONResponse({"from": "mock-target", "theme": "dark", "levels": 5})


@app.post("/api/v1/game-config")
async def game_config_post(request: Request) -> JSONResponse:
    payload = await request.json()
    return JSONResponse({"from": "mock-target", "echo": payload})


@app.get("/setcookie")
async def set_cookie() -> JSONResponse:
    response = JSONResponse({"set": True})
    response.set_cookie(
        "SID",
        "target-session-123",
        path="/",
        domain="127.0.0.1",
        httponly=True,
        samesite="none",
        secure=True,
    )
    response.set_cookie("PLAIN", "v1", path="/page2")
    return response


@app.get("/redirect")
async def redirect_internal() -> RedirectResponse:
    return RedirectResponse("/page2", status_code=302)


@app.get("/redirect-external")
async def redirect_external() -> RedirectResponse:
    return RedirectResponse("https://example.com/external", status_code=302)


@app.get("/big")
async def big() -> Response:
    chunk = b"0123456789" * 1024  # 10 KiB/块

    async def gen():
        for _ in range(200):  # 2 MB
            yield chunk

    from starlette.responses import StreamingResponse

    return StreamingResponse(gen(), media_type="application/octet-stream")


@app.get("/frame-blocked")
async def frame_blocked() -> Response:
    """带 X-Frame-Options 的页面，验证安全头是否被剥离。"""
    return Response(
        content="<html><head><title>被 frame 保护</title></head><body>ok</body></html>",
        media_type="text/html",
        headers={
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
        },
    )


@app.websocket("/ws")
async def websocket_echo(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_text(f"echo:{message}")
    except WebSocketDisconnect:
        return
