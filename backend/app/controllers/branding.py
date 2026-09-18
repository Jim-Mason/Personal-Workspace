"""品牌与外观配置接口。

`GET /api/branding` 是**免令牌**的，这是刻意的：
首页在拿到令牌之前就得先把配色、名称、Logo 渲染出来，
而这个接口只吐「平台长什么样」，不含任何本机数据。
能打开 127.0.0.1:8731 的人本来就能在页面上看到这些，不算新增泄露面。

写操作（PUT / reset）一律需要令牌 —— 免令牌白名单按「路径 + 方法」双重判断，
只放行 GET，见 security.needs_token()。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ..db import get_conn
from ..repositories import resource_repo
from ..services import branding

router = APIRouter(prefix="/api/branding", tags=["branding"])


@router.get("")
def read_branding() -> dict:
    return branding.public_payload(branding.load())


@router.put("")
def update_branding(patch: dict = Body(...)) -> dict:
    try:
        data = branding.save(patch)
    except branding.BrandingError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    with get_conn() as conn:
        resource_repo.record_event(
            conn,
            action="branding.update",
            target_type="meta",
            target_name=data.get("app_name"),
            detail={"fields": sorted(str(k) for k in patch)},
        )
    return branding.public_payload(data)


@router.post("/reset")
def reset_branding() -> dict:
    data = branding.reset()
    with get_conn() as conn:
        resource_repo.record_event(
            conn,
            action="branding.reset",
            target_type="meta",
            target_name=data.get("app_name"),
        )
    return branding.public_payload(data)
