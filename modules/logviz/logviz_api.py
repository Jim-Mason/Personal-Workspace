"""日志可视化分析：接口 + 自带页面。

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| GET | `/assets/{name}` | 页面资源（白名单） |
| GET | `/api/roots` | 日志目录名单 |
| POST | `/api/roots` | 登记一个日志目录 |
| PATCH | `/api/roots/{id}` | 改名 |
| DELETE | `/api/roots/{id}` | 移出名单（**只取消登记**） |
| GET | `/api/list` | 列一层目录（只报出像日志的文件） |
| POST | `/api/detect` | 认出这是哪种日志（不解析，先给个判断） |
| POST | `/api/analyze` | 分析，出报告 |
| GET | `/api/drill` | 下钻：某个指标对应的原始行 |
| GET | `/api/preview` | 看文件开头几行 |
| GET | `/api/cache` | 缓存用量 |
| DELETE | `/api/cache` | 清空缓存 |

### 为什么检测与解析分成两个接口

界面上"先认出这是什么，再决定怎么解析"是**两个时刻**：
用户点一个文件，先把 `?` 变成"nginx access 日志"，让他确认或改，
再真正跑解析。合并成一个接口的话，用户就没有改主意的机会了 ——
而第一版只支持三类，一定会有认错的时候。

### 没有的东西（刻意的）

没有写、删、移、改名用户的任何文件，没有下载，没有命令执行，
没有上传，不联网。分析结果只落在模块自己的缓存库里。
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import cache, context, drill, library, parsers, paths, scanner

router = APIRouter(tags=["logviz"])

#: 允许对外发送的页面资源。白名单而不是直接拼路径 ——
#: 后者一个 `..` 就能把模块目录里的 Python 源码发出去。
_ASSETS = {
    "view.css": "text/css; charset=utf-8",
    "view.js": "application/javascript; charset=utf-8",
}

_MOUNT = os.environ.get("LOCALDECK_MOUNT", "/logviz")


def _render_page() -> str:
    html = (context.WEB_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("{{MOUNT}}", _MOUNT)


def _bad(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


# ------------------------------------------------------------------ 页面
@router.get("", include_in_schema=False, response_class=HTMLResponse)
@router.get("/", include_in_schema=False, response_class=HTMLResponse)
def page() -> HTMLResponse:
    return HTMLResponse(_render_page())


@router.get("/assets/{name}", include_in_schema=False)
def asset(name: str) -> FileResponse:
    media_type = _ASSETS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="没有这个资源")
    target = context.WEB_DIR / name
    if not target.is_file():
        raise HTTPException(status_code=404, detail="资源文件缺失")
    return FileResponse(target, media_type=media_type)


# ------------------------------------------------------------------ 目录名单
class RootIn(BaseModel):
    path: str
    name: str = ""


class RootPatch(BaseModel):
    name: str


@router.get("/api/roots")
def list_roots() -> dict:
    return {"items": library.public(), "mount": _MOUNT}


@router.post("/api/roots")
def add_root(payload: RootIn) -> dict:
    try:
        return {"item": library.add(payload.path, payload.name)}
    except library.RootError as exc:
        raise _bad(exc) from exc


@router.patch("/api/roots/{root_id}")
def patch_root(root_id: str, payload: RootPatch) -> dict:
    try:
        return {"item": library.rename(root_id, payload.name)}
    except library.RootError as exc:
        raise _bad(exc) from exc


@router.delete("/api/roots/{root_id}")
def delete_root(root_id: str) -> dict:
    """把目录移出名单。

    **注意：只取消登记，磁盘上的目录一个字都不动。** 界面上也要把这句话写清楚。
    """
    if not library.remove(root_id):
        raise HTTPException(status_code=404, detail="没有这个日志目录")
    return {"ok": True, "note": "已移出名单。磁盘上的目录未做任何改动。"}


# ------------------------------------------------------------------ 找文件
def _resolve(root_id: str, rel: str) -> tuple:
    """把「目录 id + 相对路径」变成绝对路径，并保证没跑出登记范围。"""
    try:
        base = library.root_path(root_id)
    except library.RootError as exc:
        raise _bad(exc, 404) from exc
    try:
        target = paths.resolve_in_root(base, rel)
    except paths.PathNotAllowed as exc:
        raise _bad(exc) from exc
    if not target.exists():
        raise _bad("这个路径不存在", 404)
    return base, target


@router.get("/api/list")
def list_dir(root: str = Query(...), path: str = Query(""), search: str = Query("")) -> dict:
    base, target = _resolve(root, path)
    if not target.is_dir():
        raise _bad("这个路径不是一个目录")
    try:
        return paths.list_logs(base, path, search=search)
    except paths.PathNotAllowed as exc:
        raise _bad(exc) from exc


# ------------------------------------------------------------------ 检测
class FileIn(BaseModel):
    root: str
    path: str
    kind: str = ""


@router.post("/api/detect")
def detect(payload: FileIn) -> dict:
    """只认出类型，不出报告。

    刻意做成独立一步：用户得有机会改主意 —— 第一版只支持三类，
    认错是迟早的事。让他能手动挑，比让他对着错误的报告琢磨强。
    """
    _, target = _resolve(payload.root, payload.path)
    if not target.is_file():
        raise _bad("这里只接受文件")
    result = scanner.stream_lines(target)
    detected = parsers.detect(result.lines)
    return {
        "path": payload.path,
        "name": target.name,
        "size": result.file_size,
        "scan": result.to_dict(),
        **detected,
    }


# ------------------------------------------------------------------ 分析
@router.post("/api/analyze")
def analyze(payload: FileIn) -> dict:
    """跑一遍解析，返回报告。

    缓存 key 带文件大小与 mtime —— 日志一直在追加，拿旧报告当新结论
    是这个模块最危险的失败方式，所以宁可多算一次。
    """
    started = time.monotonic()
    _, target = _resolve(payload.root, payload.path)
    if not target.is_file():
        raise _bad("这里只接受文件")

    try:
        stat = target.stat()
    except OSError as exc:
        raise _bad(f"读不了这个文件：{exc}") from exc

    kind = payload.kind
    from_cache = False
    report: dict | None = None

    if kind:
        report = cache.get(target, stat.st_size, stat.st_mtime, kind)
        if report is not None:
            from_cache = True

    if report is None:
        scan = scanner.stream_lines(target)
        if not kind:
            detected = parsers.detect(scan.lines)
            kind = detected["best"]
            if not kind:
                # 认不出来就把选择权交给用户，同时把"最像哪种"告诉他
                return {
                    "detected": False,
                    "scores": detected["scores"],
                    "labels": detected["labels"],
                    "scan": scan.to_dict(),
                    "notes": [
                        "认不出这份日志的类型（三类都都不像）。"
                        "请手动指定它是哪一类 —— 认错类型出来的报告比没有报告更危险。"
                    ],
                }
        report = parsers.run(kind, scan.lines)
        report["scan"] = scan.to_dict()
        cache.put(target, stat.st_size, stat.st_mtime, kind, report)

    report["file"] = {
        "name": target.name,
        "path": str(target),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "kind": kind,
        "label": parsers.LABELS.get(kind, kind),
    }
    report["cached"] = from_cache
    report["elapsed"] = round(time.monotonic() - started, 3)
    # 报告里那一大串 score 只在下钻时有用，报告本身已经在缓存里了，
    # 这里顺手带上，界面切换类型时就不用再检测一次
    report.setdefault("notes", [])
    return report


# ------------------------------------------------------------------ 下钻
@router.get("/api/drill")
def drill_down(
    root: str = Query(...),
    path: str = Query(...),
    kind: str = Query(...),
    field: str = Query(...),
    value: str = Query(...),
    limit: int = Query(300, ge=1, le=1000),
) -> dict:
    """从报告里的某个指标回到原始行。

    带 `field=line` 时按行号取（报告里的锚点点进来走这条）。
    """
    _, target = _resolve(root, path)
    if not target.is_file():
        raise _bad("这里只接受文件")
    return drill.collect(target, kind, field, value, limit=limit)


@router.get("/api/preview")
def preview(
    root: str = Query(...),
    path: str = Query(...),
    lines: int = Query(60, ge=1, le=400),
    offset: int = Query(0, ge=0),
) -> dict:
    _, target = _resolve(root, path)
    if not target.is_file():
        raise _bad("这里只接受文件")
    return drill.preview(target, lines=lines, offset=offset)


# ------------------------------------------------------------------ 缓存
@router.get("/api/cache")
def cache_stats() -> dict:
    return cache.stats()


@router.delete("/api/cache")
def cache_clear() -> dict:
    removed = cache.clear()
    return {
        "ok": True,
        "removed": removed,
        "note": "已清空分析缓存。下次打开会重新分析，用户的日志文件未做任何改动。",
    }
