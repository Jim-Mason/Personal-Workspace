"""FastAPI 应用入口。"""

from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import config, db
# 模块一的接口已经不在这里了 —— 它作为一个 native 模块住在
# modules/inventory/，由 hub.mount_native() 挂到 /inventory 之下。
from .controllers import modules as modules_api, session as session_api, system
from .controllers import branding as branding_api
from .logging_setup import setup_logging
from .security import install_security
from .services import branding, session as sessions
from .services.modules import ModuleHub

log = logging.getLogger("localdeck")


@asynccontextmanager
async def lifespan(app: FastAPI):
    applied = db.run_migrations()
    if applied:
        log.info("已应用迁移：%s", ", ".join(applied))

    # 过期会话顺手清一次。这是个单机服务，不值得为它上定时任务 ——
    # 每次启动清一遍，永远不会有积压。
    dropped = sessions.purge_expired()
    if dropped:
        log.info("清理了 %d 条过期会话", dropped)

    hub: ModuleHub = app.state.hub
    await hub.startup()
    log.info(
        "中台已就绪 —— 版本 %s，端口 %s，数据目录 %s，日志 %s",
        config.APP_VERSION,
        config.PORT,
        config.DATA_DIR,
        config.LOG_PATH,
    )
    try:
        yield
    finally:
        # 必须先收模块再退出：中台是被 Ctrl+C 掉的，子进程若没人管就会
        # 留下来占着内部端口，下次启动直接变成「端口被占用」。
        await hub.shutdown()
        log.info("中台已停止")


def render_index() -> str:
    """把品牌配置注入首页模板。

    为什么不用「前端拿到配置再改样式」：那样会先闪一下出厂配色再跳成用户的，
    视觉上很难看。服务端直接写进 <style>，首屏就是对的。
    """
    template = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    data = branding.load()

    slots = {
        "app_name": html.escape(data["app_name"] or config.APP_NAME),
        "tagline": html.escape(data["tagline"] or ""),
        "logo_text": html.escape(data["logo_text"] or "PW"),
        "mode": html.escape(data["mode"]),
        "bg_style": html.escape(data["bg_style"]),
        "version": html.escape(config.APP_VERSION),
        # 登录门上要写「之后 N 天内不用再输」，N 从配置来，别在 HTML 里写死。
        "session_ttl": str(config.SESSION_TTL_DAYS),
    }
    for key, value in slots.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template.replace("<!--BRANDING_CSS-->", f"<style>{branding.to_css(data)}</style>")


def _session_check(token: str):
    """浏览器会话校验。

    把令牌闭包进来，不每个请求都去读一次 `data/.token` ——
    服务跑起来之后令牌不会再变，反复戳磁盘纯属浪费。
    """

    def check(request: Request) -> bool:
        sid = request.cookies.get(config.SESSION_COOKIE) or ""
        return sessions.verify(sid, token)

    return check


def create_app() -> FastAPI:
    # 日志要第一个装：从这一行之后的每一句 log.xxx 才有地方去。
    setup_logging()
    token = config.get_or_create_token()
    app = FastAPI(
        title=config.APP_NAME,
        version=config.APP_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # 会话控制器要用它比对令牌，放 state 上让控制器直接取。
    app.state.token = token

    # ---- 模块中枢：必须在装安全中间件之前建好，安全层要用它的票据表
    hub = ModuleHub()
    app.state.hub = hub
    modules_api.bind(hub)

    install_security(
        app,
        token,
        ticket_lookup=hub.ticket_lookup,
        session_verify=_session_check(token),
    )
    app.include_router(system.router)
    app.include_router(branding_api.router)
    app.include_router(session_api.router)
    app.include_router(modules_api.router)

    # native / static 模块的路由必须现在挂好，不能等到 lifespan ——
    # 那时服务已经在接受请求了。模块一的 /inventory 就在这一步挂上。
    hub.mount_native(app)
    hub.mount_static(app)

    # 静态资源走显式路由，不用 StaticFiles 挂载：
    # 挂载点的鉴权依赖容易失效，而且这几个文件必须免令牌才能把页面渲染出来。
    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(render_index())

    @app.get("/style.css")
    async def styles() -> FileResponse:
        return FileResponse(config.WEB_DIR / "style.css", media_type="text/css")

    @app.get("/app.js")
    async def script() -> FileResponse:
        return FileResponse(config.WEB_DIR / "app.js", media_type="application/javascript")

    @app.get("/favicon.svg")
    async def favicon() -> Response:
        """标签页图标跟着品牌走：渐变底 + Logo 文字，不用准备图片文件。"""
        data = branding.load()
        label = html.escape((data.get("logo_text") or "PW")[:3])
        accent = data.get("accent") or "#6366f1"
        accent2 = data.get("accent2") or "#22d3ee"
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{accent}"/>'
            f'<stop offset="1" stop-color="{accent2}"/>'
            "</linearGradient></defs>"
            '<rect width="64" height="64" rx="15" fill="url(#g)"/>'
            '<text x="32" y="43" text-anchor="middle" font-size="26" font-weight="600" '
            f'font-family="Segoe UI,Roboto,sans-serif" fill="#ffffff">{label}</text>'
            "</svg>"
        )
        return Response(svg, media_type="image/svg+xml")

    # ---- 模块代理兜底路由
    # 必须放在最后：FastAPI 按注册顺序匹配，这样 /api/* 与上面的静态路由
    # 会先命中，剩下的路径才归模块。好处是模块 mount 可以随时增删，
    # 不必在中台启动时就把路由表钉死。
    app.add_api_route(
        "/{full_path:path}",
        modules_api.build_proxy_route(),
        methods=modules_api.PROXY_METHODS,
        include_in_schema=False,
    )

    return app


app = create_app()
