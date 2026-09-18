"""本地文件列表：接口 + 自带页面。

### 为什么这个模块自己托管页面

它挂在 `/filelist` 下，`kind=native` —— 路由直接进中台的 FastAPI 应用，所以
HTML / CSS / JS 都由本模块提供（`web/` 目录），不必往中台首页里塞代码。
好处是「装一个模块的目录，这个模块就连界面一起有了」，卸载就是把目录删掉。

页面里的地址一律用 `{{MOUNT}}` 占位、由这里在返回时替换成真实挂载点 ——
写死 `/filelist` 的话，改个挂载点页面就整片 404，而且是那种「HTML 打得开、
CSS 拿不到」的半死状态。

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| GET | `/assets/{name}` | 页面资源（白名单，只发 `web/` 下这三个文件） |
| GET | `/api/roots` | 书库列表 |
| POST | `/api/roots` | 登记一个根目录 |
| PATCH | `/api/roots/{id}` | 改名 |
| DELETE | `/api/roots/{id}` | 移出书库（**只是取消登记，不碰磁盘上的目录**） |
| GET | `/api/entries` | 列一层目录 |
| GET | `/api/size` | 递归统计某层占用（有预算上限） |

### 没有的东西（刻意的）

没有写、删、移、改名，没有下载，没有打开文件，没有读取文件内容，没有命令执行。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import context, library, lister

router = APIRouter(tags=["filelist"])

#: 允许对外发送的页面资源。白名单而不是直接拼路径 ——
#: 后者一个 `..` 就能把模块目录里的 Python 源码发出去。
_ASSETS = {
    "view.css": "text/css; charset=utf-8",
    "view.js": "application/javascript; charset=utf-8",
}

_MOUNT = os.environ.get("LOCALDECK_MOUNT", "/filelist")


def _render_page() -> str:
    html = (context.WEB_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("{{MOUNT}}", _MOUNT)


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


class RootIn(BaseModel):
    path: str
    name: str = ""


class RootPatch(BaseModel):
    name: str


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


# ------------------------------------------------------------------ 书库
@router.get("/api/roots")
def list_roots() -> dict:
    return {"items": library.public(), "mount": _MOUNT}


@router.post("/api/roots")
def add_root(payload: RootIn) -> dict:
    try:
        return {"item": library.add(payload.path, payload.name)}
    except library.LibraryError as exc:
        raise _bad(exc) from exc


@router.patch("/api/roots/{root_id}")
def patch_root(root_id: str, payload: RootPatch) -> dict:
    try:
        return {"item": library.rename(root_id, payload.name)}
    except library.LibraryError as exc:
        raise _bad(exc) from exc


@router.delete("/api/roots/{root_id}")
def delete_root(root_id: str) -> dict:
    """把书库移出列表。

    **注意：只取消登记，磁盘上的目录一个字都不动。** 界面上也要把这句话写清楚，
    否则用户会以为"移除"等于"删掉"。
    """
    if not library.remove(root_id):
        raise HTTPException(status_code=404, detail="没有这个书库")
    return {"ok": True, "note": "已移出书库。磁盘上的目录未做任何改动。"}


# ------------------------------------------------------------------ 浏览
@router.get("/api/entries")
def entries(
    root: str = Query(..., description="书库 id"),
    path: str = Query("", description="相对于书库根目录的路径"),
    show_files: bool = Query(True),
    sort: str = Query("name", description="name | size | mtime | kind"),
    desc: bool = Query(False),
    search: str = Query(""),
) -> dict:
    try:
        base = library.root_path(root)
    except library.LibraryError as exc:
        raise _bad(exc) from exc
    try:
        data = lister.list_dir(
            base, path, show_files=show_files, sort=sort, desc=desc, search=search
        )
    except lister.PathNotAllowed as exc:
        raise _bad(exc) from exc
    data["root_name"] = (library.get(root) or {}).get("name", "")
    return data


@router.get("/api/size")
def size(root: str = Query(...), path: str = Query("")) -> dict:
    try:
        base = library.root_path(root)
    except library.LibraryError as exc:
        raise _bad(exc) from exc
    try:
        return lister.measure(base, path)
    except lister.PathNotAllowed as exc:
        raise _bad(exc) from exc
