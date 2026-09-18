"""日志根目录：用户登记的名单。

写法与 `modules/filelist/library.py` 一致（模块间不互相 import，所以是有意重复）。

### 为什么根目录要用户显式登记

这是本模块的安全阀。如果允许随便传绝对路径（`?path=C:\\Windows\\System32\\config`），
那就等于给了浏览器一个「读整块磁盘上任何文本文件」的接口；把可读范围限定在
用户亲手登记的几个根目录里，"能读哪里"这件事就由用户自己决定，而不是由 URL 决定。

登记时就把路径 `resolve()` 掉并存在配置里 —— 之后所有请求都用这个已解析的
绝对路径做包含性比对，避免每次都要重新归一化。

### 首次运行的种子

刻意**不**自动登记 `C:\\` 或整个用户目录 —— 日志目录通常很具体
（`D:\\logs`、`/var/log`、应用自己的 `logs/`）。自动划进一大片，
等于把这个安全阀拆掉一半。所以首次运行时给的是**空的**，
界面上直接引导用户登记一个目录。
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from . import context

_SCHEMA_VERSION = 1


class RootError(Exception):
    """可以展示给用户看的失败原因。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> list[dict]:
    path = context.ROOTS_FILE
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 配置坏了不该让整个模块打不开：回到空列表，用户重新登记即可
        return []
    roots = raw.get("roots")
    if not isinstance(roots, list):
        return []
    return [item for item in roots if isinstance(item, dict) and item.get("path")]


def _store(roots: list[dict]) -> None:
    context.ensure_dirs()
    payload = {"version": _SCHEMA_VERSION, "roots": roots}
    tmp = context.ROOTS_FILE.with_suffix(".json.tmp")
    # 先写临时文件再原子替换：中途断电也不会留下一个半截的配置文件
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, context.ROOTS_FILE)


def get(root_id: str) -> dict | None:
    for item in load():
        if item.get("id") == root_id:
            return item
    return None


def root_path(root_id: str) -> Path:
    item = get(root_id)
    if item is None:
        raise RootError("没有这个日志目录")
    return Path(item["path"])


def add(path_text: str, name: str = "") -> dict:
    raw = (path_text or "").strip().strip('"')
    if not raw:
        raise RootError("请填一个目录路径")

    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RootError(f"这个路径打不开：{exc}") from exc
    if not resolved.is_dir():
        raise RootError("这里只接受目录，不接受单个文件")

    roots = load()
    key = os.path.normcase(str(resolved))
    for item in roots:
        if os.path.normcase(item["path"]) == key:
            raise RootError("这个目录已经在名单里了")

    entry = {
        "id": secrets.token_hex(4),
        "name": (name or "").strip() or resolved.name or str(resolved),
        "path": str(resolved),
        "added_at": _now(),
    }
    roots.append(entry)
    _store(roots)
    return entry


def rename(root_id: str, name: str) -> dict:
    clean = (name or "").strip()
    if not clean:
        raise RootError("名字不能为空")
    roots = load()
    for item in roots:
        if item.get("id") == root_id:
            item["name"] = clean[:80]
            _store(roots)
            return item
    raise RootError("没有这个日志目录")


def remove(root_id: str) -> bool:
    roots = load()
    kept = [item for item in roots if item.get("id") != root_id]
    if len(kept) == len(roots):
        return False
    _store(kept)
    return True


def public() -> list[dict]:
    """给前端的字段。

    `exists` 是现算的：登记的目录可能已经被改名或挪走（尤其外接盘 / 网络盘
    没挂上时）。让界面直接显示「这个目录现在打不开」，比点进去报一堆错好。
    """
    items = []
    for item in load():
        path = Path(item["path"])
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        items.append(
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "path": item.get("path", ""),
                "added_at": item.get("added_at", ""),
                "exists": exists,
            }
        )
    return items
