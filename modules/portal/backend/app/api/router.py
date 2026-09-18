"""API 路由聚合（统一挂在 /api 前缀下）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import auth, cards, uploads, users

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(cards.router)
api_router.include_router(uploads.router)
api_router.include_router(users.router)

__all__ = ["api_router"]
