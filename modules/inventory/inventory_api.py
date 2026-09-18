"""本机资产清单相关接口。

模块一从这里对外提供服务。迁进 `modules/inventory/` 时**对外行为一字未改** ——
接口前缀仍是 `/api/apps`，只是整体挂到了 `/inventory` 之下
（见同目录的 `module.py`），最终地址是 `/inventory/api/apps/*`。

依赖全部指向模块内部：`context`（路径与机器标识）、`store`（数据访问）、
`scanner_apps` / `launcher`（业务）。**不 import 中台的任何包** —— 这是模块
能独立存在的前提，理由见 `context.py` 顶部。

### 这里为什么有两个 router

`router`（顶层）挂根路径 `/inventory`，负责入口页；`api_router` 挂
`/inventory/api/apps`，是真正的接口。

之所以要有个入口页：中台的「模块」面板给每个模块都提供「打开」，点开就是
它挂载点的根路径。没有入口页的话，点「打开」会撞上一个 404 —— 而模块一的
界面其实住在中台的「本机资产」标签页里（它是平台的第一个模块，界面比模块
机制还早）。入口页把这件事说清楚并给个跳转，好过一个死链接。
"""

from __future__ import annotations

import json
import os
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import context, launcher, scanner_apps, store

#: 本模块挂载点，用来生成入口页里的跳转地址
_MOUNT = os.environ.get("LOCALDECK_MOUNT", "/inventory")

#: 真正的业务接口
api_router = APIRouter(prefix="/api/apps", tags=["apps"])

#: 对外暴露的唯一 router（module.py 里 router_object="router" 指的就是它）
router = APIRouter(tags=["inventory"])
router.include_router(api_router)


class ResourcePatch(BaseModel):
    alias: str | None = None
    tags: list[str] | None = None
    group_name: str | None = None
    favorite: bool | None = None
    ignored: bool | None = None
    managed: bool | None = None
    note: str | None = None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@api_router.get("")
def list_apps(
    q: str | None = Query(None, description="按名称/别名/发布者/路径模糊搜索"),
    source: str | None = Query(None, description="registry | startmenu"),
    sort: str = Query("name", description="name | size | installed | recent | used"),
    include_ignored: bool = Query(False),
    limit: int = Query(2000, ge=1, le=20000),
    offset: int = Query(0, ge=0),
) -> dict:
    with store.get_conn() as conn:
        items = store.list_resources(
            conn,
            res_type="app",
            source=source,
            query=q,
            include_ignored=include_ignored,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        total_row = conn.execute(
            "SELECT COUNT(*) FROM resource WHERE type = 'app' AND ignored = 0"
        ).fetchone()
        ignored_row = conn.execute(
            "SELECT COUNT(*) FROM resource WHERE type = 'app' AND ignored = 1"
        ).fetchone()
    for item in items:
        if item.get("tags"):
            try:
                item["tags"] = json.loads(item["tags"])
            except (TypeError, ValueError):
                item["tags"] = []
        else:
            item["tags"] = []
    return {
        "items": items,
        "returned": len(items),
        "total": total_row[0] if total_row else 0,
        "ignored_total": ignored_row[0] if ignored_row else 0,
    }


@api_router.post("/scan")
def scan_apps(request: Request) -> dict:
    """扫描本机已安装应用。全程只读，不写注册表、不动任何文件。"""
    started = time.perf_counter()
    machine_id = context.machine_id()

    with store.get_conn() as conn:
        store.record_event(conn, action="scan.start", actor="web", ip=_client_ip(request))

        try:
            result = scanner_apps.scan_all()
        except Exception as exc:  # noqa: BLE001 - 扫描失败要如实回报而不是 500 白屏
            duration = int((time.perf_counter() - started) * 1000)
            store.record_task(
                conn,
                kind="scan",
                machine_id=machine_id,
                status="failed",
                duration_ms=duration,
                message=str(exc),
            )
            store.record_event(
                conn,
                action="scan.failed",
                target_name=None,
                detail={"error": str(exc)},
                ip=_client_ip(request),
            )
            raise HTTPException(status_code=500, detail=f"扫描失败：{exc}") from exc

        registry_result = store.upsert_from_scan(
            conn, "registry", result["registry_apps"]
        )
        startmenu_result = store.upsert_from_scan(
            conn, "startmenu", result["startmenu_extra"]
        )

        duration = int((time.perf_counter() - started) * 1000)
        summary = {
            "registry": registry_result,
            "startmenu": startmenu_result,
            "scan": result["stats"],
            "duration_ms": duration,
        }
        task_id = store.record_task(
            conn,
            kind="scan",
            machine_id=machine_id,
            status="ok",
            duration_ms=duration,
            message=(
                f"注册表 {registry_result['total']} 条"
                f"（新增 {registry_result['inserted']} / 更新 {registry_result['updated']}），"
                f"开始菜单补充 {startmenu_result['total']} 条"
            ),
        )
        store.record_event(
            conn,
            action="scan.finish",
            detail=summary,
            ip=_client_ip(request),
        )

    return {"task_id": task_id, **summary}


@api_router.post("/{resource_id}/launch")
def launch_app(resource_id: int, request: Request) -> dict:
    machine_id = context.machine_id()
    with store.get_conn() as conn:
        resource = store.get_resource(conn, resource_id)
        if not resource:
            raise HTTPException(status_code=404, detail="资源不存在")
        result = launcher.launch(
            conn, resource, machine_id=machine_id, ip=_client_ip(request)
        )
    if result["status"] != "ok":
        raise HTTPException(status_code=400, detail=result.get("message") or "启动失败")
    return result


@api_router.patch("/{resource_id}")
def patch_app(resource_id: int, payload: ResourcePatch, request: Request) -> dict:
    changes = payload.model_dump(exclude_none=True)
    if "tags" in changes:
        changes["tags"] = json.dumps(changes["tags"], ensure_ascii=False)
    for flag in ("favorite", "ignored", "managed"):
        if flag in changes:
            changes[flag] = 1 if changes[flag] else 0

    with store.get_conn() as conn:
        before = store.get_resource(conn, resource_id)
        if not before:
            raise HTTPException(status_code=404, detail="资源不存在")
        after = store.update_user_fields(conn, resource_id, changes)
        # 忽略等价于「从我的清单里拿掉」，单独记一条更好回溯。
        action = "app.ignore" if changes.get("ignored") == 1 else "app.update"
        store.record_event(
            conn,
            action=action,
            target_type=before.get("type"),
            target_id=resource_id,
            target_name=before.get("name"),
            detail={"before": {k: before.get(k) for k in changes}, "after": changes},
            ip=_client_ip(request),
        )
    return {"item": after}


# ------------------------------------------------------------------ 入口页
@router.get("", include_in_schema=False, response_class=HTMLResponse)
@router.get("/", include_in_schema=False, response_class=HTMLResponse)
def landing() -> HTMLResponse:
    """模块入口页。

    模块一的界面住在中台的「本机资产」标签页里 —— 它比模块机制更早存在，
    界面是直接长在平台外壳上的。所以这里不做第二套界面，而是把话说清楚
    并给一个跳转：模块面板上的「打开」点进来不该是个 404。
    """
    total = 0
    try:
        with store.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM resource WHERE type = 'app' AND ignored = 0"
            ).fetchone()
            total = row[0] if row else 0
    except Exception:  # noqa: BLE001 - 入口页不该因为数据读不到就整页报错
        total = 0

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>本机资产清单</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;
         background:#f7f5f0; color:#26221c; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#1a1917; color:#ece7de; }}
    .card {{ background:#232120 !important; border-color:#38342f !important; }}
    .note {{ color:#a9a196 !important; }}
    a.go {{ background:#d9a273 !important; border-color:#d9a273 !important; color:#1a1917 !important; }}
  }}
  .card {{ width:min(520px,92vw); background:#fffdf8; border:1px solid #e2dbcd;
          border-radius:14px; padding:26px 28px; }}
  h1 {{ margin:0 0 6px; font-size:19px; }}
  .note {{ color:#6b6357; font-size:13.5px; line-height:1.7; margin:0 0 16px; }}
  .num {{ font-size:34px; font-weight:600; margin:2px 0 4px; }}
  .num small {{ font-size:13px; font-weight:400; color:#9c9384; margin-left:6px; }}
  a.go {{ display:inline-block; margin-top:6px; padding:8px 16px; border-radius:8px;
         background:#8a5a2b; border:1px solid #8a5a2b; color:#fff;
         text-decoration:none; font-size:13.5px; }}
  code {{ font-family:ui-monospace,Consolas,monospace; font-size:12.5px; }}
</style>
</head>
<body>
  <div class="card">
    <h1>本机资产清单</h1>
    <p class="note">
      这个模块的界面在中台的「<strong>本机资产</strong>」标签页里，
      而不是一个独立的页面 —— 它比模块机制更早存在。
    </p>
    <div class="num">{total}<small>条已纳管的程序</small></div>
    <p class="note">
      接口挂载点：<code>{_MOUNT}/api/apps</code><br>
      扫描全程只读，不写注册表、不移动/删除/改名任何文件。
    </p>
    <a class="go" href="/?tab=apps">回到中台的「本机资产」 →</a>
  </div>
</body>
</html>"""
    return HTMLResponse(html)
