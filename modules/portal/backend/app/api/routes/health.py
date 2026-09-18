"""健康检查 / 就绪检查端点。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.db.session import check_connection

router = APIRouter(tags=["health"])


@router.get("/health", summary="存活检查（进程是否在跑）")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.env,
        "version": settings.app_version,
    }


@router.get("/ready", summary="就绪检查（依赖是否可用）")
async def ready() -> JSONResponse:
    db_ok = check_connection()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "checks": {
            "database": "ok" if db_ok else "failed",
            "proxy_enabled": settings.proxy_enabled,
        },
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)
