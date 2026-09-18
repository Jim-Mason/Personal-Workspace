"""书库：用户登记的根目录。

配置写在**模块自己的** `data/libraries.json` 里，不动中台的库，也不动用户的
任何文件 —— 这里唯一会写的，是这个模块自己家里的一份配置。

### 为什么根目录要用户显式登记

这是本模块的安全阀。如果允许随便传绝对路径（`?path=C:\\Windows\\System32`），
那就等于给了浏览器一个「遍历整块磁盘」的接口；把可读范围限定在用户亲手登记的
几个根目录里，"能看哪里"这件事就由用户自己决定，而不是由 URL 决定。

登记时就把路径 `resolve()` 掉并存在配置里 —— 之后所有请求都用这个已解析的
绝对路径做包含性比对，避免每次都要重新归一化。
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from . import context

_SCHEMA_VERSION = 1


class LibraryError(Exception):
    """可以展示给用户看的失败原因。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _seed() -> list[dict]:
    """首次运行给一个能用的起点：用户自己的主目录。

    刻意只给一个，而且是最没有争议的那个 —— 别人机器上跑起来也能立刻看到
    东西，又不会擅自把整块磁盘都划进来。
    """
    try:
        home = Path.home().resolve()
    except OSError:
        return []
    if not home.is_dir():
        return []
    return [
        {
            "id": secrets.token_hex(4),
            "name": home.name or str(home),
            "path": str(home),
            "added_at": _now(),
        }
    ]


def load() -> list[dict]:
    path = context.LIBRARIES_FILE
    if not path.is_file():
        roots = _seed()
        if roots:
            _store(roots)
        return roots
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
    tmp = context.LIBRARIES_FILE.with_suffix(".json.tmp")
    # 先写临时文件再原子替换：中途断电也不会留下一个半截的配置文件
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, context.LIBRARIES_FILE)


def get(root_id: str) -> dict | None:
    for item in load():
        if item.get("id") == root_id:
            return item
    return None


def root_path(root_id: str) -> Path:
    item = get(root_id)
    if item is None:
        raise LibraryError("没有这个书库")
    return Path(item["path"])


def add(path_text: str, name: str = "") -> dict:
    raw = (path_text or "").strip().strip('"')
    if not raw:
        raise LibraryError("请填一个目录路径")

    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LibraryError(f"这个路径打不开：{exc}") from exc
    if not resolved.is_dir():
        raise LibraryError("这里只接受目录，不接受单个文件")

    roots = load()
    key = os.path.normcase(str(resolved))
    for item in roots:
        if os.path.normcase(item["path"]) == key:
            raise LibraryError("这个目录已经在书库里了")

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
        raise LibraryError("名字不能为空")
    roots = load()
    for item in roots:
        if item.get("id") == root_id:
            item["name"] = clean[:80]
            _store(roots)
            return item
    raise LibraryError("没有这个书库")


def remove(root_id: str) -> bool:
    roots = load()
    kept = [item for item in roots if item.get("id") != root_id]
    if len(kept) == len(roots):
        return False
    _store(kept)
    return True


def public() -> list[dict]:
    """给前端的字段。

    `exists` 是现算的：书库指向的目录可能已经被改名或挪走（尤其外接盘 / 网络盘
    没挂上时）。让界面直接显示「这个书库现在打不开」，比点进去报一堆错好。
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
