"""模块相关接口 + 反向代理入口。

分两块：

- `/api/modules*` —— 给前端「模块」面板用：状态、启停、日志、重载声明
- `/{full_path:path}` —— 兜底的模块代理入口。

兜底路由必须**最后注册**（见 main.py）。FastAPI 按注册顺序匹配，
把它放在最后，`/api/*` 和首页那些具体路由会先命中，剩下的才轮到模块。
好处是模块的 mount 可以随时增删 —— 不用在中台启动时就把路由表钉死。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..services.modules import ModuleHub, ModuleStartError

router = APIRouter(prefix="/api/modules", tags=["modules"])

# 由 main.py 在装配时注入，避免控制器反向依赖应用对象
_hub: ModuleHub | None = None


def bind(hub: ModuleHub) -> None:
    global _hub
    _hub = hub


def _require_hub() -> ModuleHub:
    if _hub is None:  # pragma: no cover - 装配错误属于开发期问题
        raise HTTPException(status_code=500, detail="模块中枢未装配")
    return _hub


# ------------------------------------------------------------------ 面板接口


@router.get("")
def list_modules() -> dict:
    return _require_hub().public_list()


@router.post("/reload")
def reload_modules() -> dict:
    hub = _require_hub()
    result = hub.reload()
    return {
        "loaded": result["loaded"],
        "errors": result["errors"],
        "modules": hub.public_list()["items"],
    }


@router.post("/{module_id}/start")
def start_module(module_id: str) -> dict:
    try:
        result = _require_hub().start(module_id)
    except ModuleStartError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "module": _module_entry(module_id)}


@router.post("/{module_id}/stop")
def stop_module(module_id: str) -> dict:
    try:
        result = _require_hub().stop(module_id)
    except ModuleStartError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "module": _module_entry(module_id)}


@router.post("/{module_id}/restart")
def restart_module(module_id: str) -> dict:
    try:
        result = _require_hub().restart(module_id)
    except ModuleStartError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "module": _module_entry(module_id)}


@router.get("/{module_id}/log")
def module_log(module_id: str, lines: int = Query(200, ge=1, le=2000)) -> dict:
    try:
        return _require_hub().log_tail(module_id, lines=lines)
    except ModuleStartError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _module_entry(module_id: str) -> dict | None:
    for item in _require_hub().public_list()["items"]:
        if item["id"] == module_id:
            return item
    return None


# ------------------------------------------------------------------ 代理入口

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def build_proxy_route():
    """返回兜底代理的 handler。放在 main.py 里注册，确保它排在最后。"""

    async def _proxy(request: Request, full_path: str) -> Response:
        hub = _require_hub()
        found = hub.registry.by_mount("/" + full_path)
        if found is None:
            # 走到这里说明既不是中台的接口、也不是任何模块的路径
            return JSONResponse(
                {"detail": f"没有匹配的路径：/{full_path}"}, status_code=404
            )
        spec, rest = found

        # /opsgen → /opsgen/：Flask 的相对链接、以及我们注入的垫片都假设
        # 当前目录以 / 结尾。不做这一步，页面里的相对路径会往上退一级。
        if rest == "" and not request.url.path.endswith("/"):
            query = request.url.query
            target = f"{spec.mount}/" + (f"?{query}" if query else "")
            return RedirectResponse(target, status_code=307)

        return await hub.proxy.handle(request, spec.id, rest)

    return _proxy
