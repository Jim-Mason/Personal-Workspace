"""FastAPI 应用入口。

启动顺序：初始化日志 → 跑迁移 → 创建初始管理员 → 注册中间件与路由 → 挂载前端产物。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.api.routes.gateway import (
    authenticate_request,
    resolve_target,
    router as gateway_router,
)
from app.api.routes.uploads import assets_router as upload_assets_router
from app.core.config import PROJECT_DIR, settings
from app.core.errors import register_exception_handlers
from app.core.logging import (
    get_request_id,
    new_request_id,
    reset_request_id,
    set_request_id,
    setup_logging,
)
from app.db.session import SessionLocal
from app.models.entities import OPEN_MODE_PROXY
from app.services.proxy_service import (
    gateway,
    gateway_slug_from_referer,
    is_portal_owned_path,
)

logger = logging.getLogger("app")

FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"

# 兜底路由接受的方法（故意不含 OPTIONS：未匹配的预检请求应当老实返回 405）
_FALLBACK_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]

# 门户自身响应加的安全头（网关响应不加，避免影响被代理系统）
_PORTAL_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

# 上传的静态资源额外加固：
# - CSP 用 default-src 'none' + sandbox，万一有文件被解析成可执行类型也跑不起来
# - 长缓存（文件名是 uuid，内容永不重复）；用 private 是因为图标读取需要登录，
#   不能让共享缓存把「已登录才能看」的内容缓存下来发给别人
_UPLOAD_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "private, max-age=31536000, immutable",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.services.bootstrap import bootstrap

    logger.info("启动 %s v%s (env=%s)", settings.app_name, settings.app_version, settings.env)
    bootstrap()
    logger.info("数据库: %s", settings.db_path or settings.database_url)
    logger.info("反向代理: %s", "已启用" if settings.proxy_enabled else "已关闭")
    if not FRONTEND_DIST.exists():
        logger.warning("未找到前端构建产物 %s，请先执行前端构建", FRONTEND_DIST)
    yield
    await gateway.aclose()
    logger.info("服务已停止")


def _spa_response(full_path: str) -> Response:
    """SPA 兜底：命中前端路由就返回入口，未匹配的接口路径返回规范 404。"""
    if full_path.startswith(("api/", "gw/", "uploads/")):
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "not_found",
                    "message": f"接口不存在：/{full_path}",
                    "request_id": get_request_id(),
                }
            },
        )

    index = FRONTEND_DIST / "index.html"
    candidate = (FRONTEND_DIST / full_path).resolve() if full_path else None

    if candidate and FRONTEND_DIST in candidate.parents and candidate.is_file():
        return FileResponse(candidate)

    if index.is_file():
        return FileResponse(index, media_type="text/html")

    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "frontend_not_built",
                "message": "前端尚未构建。请在 frontend 目录执行 npm install && npm run build。",
                "request_id": get_request_id(),
            }
        },
    )


async def _fallback_proxy(request: Request, slug: str, path: str) -> Response | None:
    """按 Referer 推断出的 slug 兜底转发；条件不满足时返回 None 交回 SPA 逻辑。"""
    if not settings.proxy_enabled:
        return None

    with SessionLocal() as db:
        # 与 /gw/{slug}/ 网关共用同一个解析器：标识可能指向卡片本体，
        # 也可能指向卡片下的一条环境地址；两边必须一致，否则兜底转发会漏掉
        # 「环境后缀路径」那类请求（它们只带 Referer，不带 /gw/ 前缀）。
        target, card = resolve_target(db, slug)
        if target is None or card is None:
            return None
        if not card.enabled or target.open_mode != OPEN_MODE_PROXY:
            return None

        logger.info(
            "网关兜底转发 slug=%s path=/%s referer=%s",
            slug,
            path,
            request.headers.get("referer", ""),
        )
        return await gateway.handle_http(request, target, path)


def create_app() -> FastAPI:
    setup_logging(level=settings.log_level, as_json=settings.log_json)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="内网工具导航门户 + 反向代理网关",
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # ---------------- 中间件 ----------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001
        request_id = request.headers.get("x-request-id") or new_request_id()
        token = set_request_id(request_id)
        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:  # noqa: BLE001 - 交给全局异常处理器兜底
            elapsed = (time.perf_counter() - started) * 1000
            logger.exception(
                "请求异常 method=%s path=%s cost_ms=%.1f",
                request.method,
                request.url.path,
                elapsed,
            )
            reset_request_id(token)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id

        # 网关流量单独记日志：便于排查"某个内网系统特别慢"
        if request.url.path.startswith("/gw/"):
            logger.info(
                "网关转发 method=%s path=%s status=%s cost_ms=%.1f",
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
            )
        else:
            logger.info(
                "请求完成 method=%s path=%s status=%s cost_ms=%.1f",
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
            )

        if request.url.path.startswith("/gw/"):
            pass  # 网关响应保持上游原样
        elif request.url.path.startswith("/uploads/"):
            for key, value in _UPLOAD_SECURITY_HEADERS.items():
                response.headers.setdefault(key, value)
        else:
            for key, value in _PORTAL_SECURITY_HEADERS.items():
                response.headers.setdefault(key, value)

        reset_request_id(token)
        return response

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            # X-Card-Count 让导出接口在不解析响应体的情况下报告卡片数量
            expose_headers=["X-Request-ID", "X-Card-Count"],
        )

    # ---------------- 路由 ----------------
    app.include_router(health_router)
    app.include_router(api_router)
    app.include_router(gateway_router)

    # ---------------- 前端静态资源 ----------------
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    # ---------------- 卡片图标（用户上传） ----------------
    # 走显式路由而不是 StaticFiles 挂载：静态挂载无法叠加鉴权依赖，
    # 会让上传的图标变成"知道文件名就能看"的公网资源。
    # 当前策略与其他业务接口一致 —— 必须登录（未登录 401）。
    # 文件名由服务端生成（uuid + 白名单扩展名），目录下不存在可执行文件；
    # 响应头在 request_context 中间件里按 /uploads/ 前缀加固（nosniff + CSP sandbox + 长缓存）。
    if settings.uploads_enabled:
        app.include_router(upload_assets_router)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        icon = FRONTEND_DIST / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)

    # ---------------- 兜底路由（必须最后注册） ----------------
    @app.api_route(
        "/{full_path:path}", methods=_FALLBACK_METHODS, include_in_schema=False
    )
    async def fallback(full_path: str, request: Request) -> Response:
        """未匹配任何路由的请求，两条出路。

        1. **网关兜底转发** —— 请求来自某个已打开的内网页面
           （`Referer` 指向 `/gw/<slug>/`）且不属于门户自身命名空间时，按该 slug 转发。
           这是为了接住"内网页面的 JS 用根路径拼出来的请求"：
           HTML 里的 `src="/x"` 由字节级重写处理、`fetch("/x")` 由客户端 shim 处理，
           但 `location.href = "/api/v1/x"` 这类顶层导航两者都拦不住
           （`window.location` 是 [Unforgeable]，客户端改不了）。
           没有这条兜底，那些接口调用会打到门户域名根上，收到一个莫名其妙的 404。

        2. **SPA 兜底** —— 其余情况返回前端入口或规范 404。

        安全性：`Referer` 是客户端可控的，所以**必须先过网关鉴权**，
        再要求"确实存在、已启用、且为代理模式"的卡片；
        `is_portal_owned_path` 保证门户自身接口绝不会被转发出去。
        """
        slug = gateway_slug_from_referer(request.headers.get("referer", ""))
        if slug and not is_portal_owned_path(full_path):
            authenticate_request(request)  # 未经登录的伪造 Referer 在这里被挡掉
            proxied = await _fallback_proxy(request, slug, full_path)
            if proxied is not None:
                return proxied
        return _spa_response(full_path)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=not settings.is_production,
    )
