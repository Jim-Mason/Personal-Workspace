"""概览、审计流水、任务历史、健康检查。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..config import APP_NAME, APP_VERSION, get_or_create_machine_id
from ..db import get_conn
from ..repositories import resource_repo

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health() -> dict:
    with get_conn() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True, "app": APP_NAME, "version": APP_VERSION}


@router.get("/overview")
def overview() -> dict:
    with get_conn() as conn:
        data = resource_repo.overview(conn)
        data["events"] = resource_repo.recent_events(conn, limit=12)
    data["machine_id"] = get_or_create_machine_id()
    return data


@router.get("/events")
def events(limit: int = Query(50, ge=1, le=500)) -> dict:
    with get_conn() as conn:
        items = resource_repo.recent_events(conn, limit=limit)
    return {"items": items, "returned": len(items)}


@router.get("/tasks")
def tasks(limit: int = Query(50, ge=1, le=500)) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(row) for row in rows], "returned": len(rows)}
