"""只读目录列举。

### ⛔ 三条不许放宽的约束

1. **全程只读。** 本文件里没有任何写操作 —— 不开写模式、不建、不删、不移、
   不改名。想加功能时先问自己：这个动作会改动用户的文件系统吗？会，就不加。

2. **路径必须落在登记的根目录之内。** 所有入口都走 `resolve_in_root()`：
   拒 `..`、拒绝对路径、拒盘符；解析完再拿 `resolve()` **解开符号链接**重新
   比对一次 —— 少了这一步，根目录里一个指向 `C:\\Windows` 的快捷方式就能
   把整条路径带出去。

3. **不读文件内容。** 这里只取元数据（名字、类型、大小、修改时间）。
   内容读取是另一件事，将来要做也必须单独设计确认流程。

### 为什么判断用 `normcase` 而不是直接比字符串

Windows 路径大小写不敏感（`C:\\Data` 与 `c:\\data` 是同一个地方）。
不做归一化就会出现"明明在根里却被判成越界"，或者更糟：绕过检查。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

#: 单层目录最多返回这么多条，防的是把内存和浏览器一起打爆
MAX_ENTRIES = 5000
#: 每层最多为多少个**子目录**去数它的条目数（数一次 = 一次 scandir）
COUNT_BUDGET = 400
#: 递归统计大小的时间预算（秒）。超了就如实告诉用户"没算完"
SIZE_TIME_BUDGET = 8.0
#: 递归统计大小最多走这么多条
SIZE_ENTRY_BUDGET = 200_000

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PathNotAllowed(Exception):
    """路径越出了登记的根目录，或者本身就不是合法相对路径。"""


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def within(root: Path, target: Path) -> bool:
    """target 是否在 root 之内（含 root 自身）。"""
    r = _norm(root)
    t = _norm(target)
    return t == r or t.startswith(r.rstrip(os.sep) + os.sep)


def resolve_in_root(root: Path, rel: str) -> Path:
    """把相对路径解析成绝对路径，并保证它没跑出 root。

    `root` 必须是**已经 resolve 过的**绝对目录（登记时就做掉）。
    """
    if rel and "\x00" in rel:
        raise PathNotAllowed("路径里有非法字符")
    raw = (rel or "").replace("\\", "/").strip()
    if raw.startswith("/") or _DRIVE_RE.match(raw):
        raise PathNotAllowed(f"这里只接受相对路径，不接受 {rel!r}")

    parts: list[str] = []
    for piece in raw.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            raise PathNotAllowed("路径里不允许出现 ..")
        parts.append(piece)

    candidate = root.joinpath(*parts) if parts else root
    # 关键一步：resolve() 会把符号链接解开。根目录里放一个指向外面的链接，
    # 上一步的「没有 ..」是拦不住的 —— 必须在解开之后再比对一次。
    resolved = candidate.resolve(strict=False)
    if not within(root, resolved):
        raise PathNotAllowed(f"路径跑到登记的书库之外了：{rel!r}")
    return resolved


def _natural_key(text: str):
    """自然序：让「第 10 章」排在「第 9 章」后面，而不是按字符序排到前面。"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


_SORTERS = {
    "name": lambda item: (_natural_key(item["name"]),),
    "size": lambda item: (item["size"], _natural_key(item["name"])),
    "mtime": lambda item: (item["mtime"], _natural_key(item["name"])),
    "kind": lambda item: (item["ext"], _natural_key(item["name"])),
}


def _is_reparse(entry: os.DirEntry) -> bool:
    """这个条目是不是「链接」（符号链接 / junction）。

    Windows 上 junction 的 `is_symlink()` 返回 False，只能靠 lstat 的
    reparse tag 认出来；只判 is_symlink 会漏掉一整类目录链接。
    """
    try:
        if entry.is_symlink():
            return True
        return bool(getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0))
    except OSError:
        return False


def _entry(
    target: Path,
    name: str,
    rel: str,
    *,
    counts: bool,
    is_dir: bool,
    outside: bool,
    is_link: bool,
) -> dict:
    item = {
        "name": name,
        "path": rel,
        "is_dir": is_dir,
        "size": 0,
        "mtime": 0.0,
        "ext": "",
        "child_count": None,
        "outside": outside,
        "is_link": is_link,
        "unreadable": False,
    }
    if outside:
        # 指向书库之外的链接：**连 stat 都不做**。
        # 数一数它的条目数、读一下它的大小，都是"读了根目录之外的东西" ——
        # 哪怕只是一个数字，也不该发生。界面上把它标成「外部链接」，
        # 点进去会得到一句明确的拒绝。
        return item
    try:
        st = target.stat()  # 只 stat，不打开
    except OSError:
        item["unreadable"] = True
        return item
    item["mtime"] = st.st_mtime
    if is_dir:
        if counts:
            item["child_count"] = _count_children(target)
    else:
        item["size"] = st.st_size
        item["ext"] = target.suffix.lower().lstrip(".")
    return item


def _count_children(folder: Path) -> int | None:
    """数一个目录里有多少条。数不动就返回 None，别让一层卡死整个页面。"""
    n = 0
    try:
        with os.scandir(folder) as it:
            for _ in it:
                n += 1
                if n > MAX_ENTRIES:
                    return MAX_ENTRIES  # 到顶就报这个数，前端会显示成「5000+」
    except OSError:
        return None
    return n


def list_dir(
    root: Path,
    rel: str,
    *,
    show_files: bool = True,
    sort: str = "name",
    desc: bool = False,
    search: str = "",
) -> dict:
    """列一层目录。返回条目、本层小计与是否被截断。"""
    target = resolve_in_root(root, rel)
    if not target.is_dir():
        raise PathNotAllowed("这个路径不是一个目录")

    raw_entries: list[tuple[str, Path, str, bool, bool, bool]] = []
    truncated = False
    try:
        with os.scandir(target) as it:
            for entry in it:
                if len(raw_entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                # follow_symlinks=False：不跟着链接判断类型，
                # 否则一个指向根外的目录链接会被当成"根内的目录"
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not show_files:
                    continue
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                # 链接在这里就解开判定一次，后续所有环节都用这个结论 ——
                # 判类型、数条目、给大小，全都不再碰根外的东西。
                resolved = Path(entry.path).resolve(strict=False)
                outside = not within(root, resolved)
                raw_entries.append(
                    (entry.name, Path(entry.path), child_rel, is_dir, outside, _is_reparse(entry))
                )
    except PermissionError as exc:
        raise PathNotAllowed("没有权限读取这个目录") from exc
    except OSError as exc:
        raise PathNotAllowed(f"无法读取这个目录：{exc}") from exc

    needle = (search or "").strip().lower()
    if needle:
        raw_entries = [row for row in raw_entries if needle in row[0].lower()]

    # 先按名字排一遍再数子目录条目数：数的时候顺便只对前 COUNT_BUDGET 个动手
    raw_entries.sort(key=lambda row: _natural_key(row[0]))
    items: list[dict] = []
    for index, (name, path, child_rel, is_dir, outside, is_link) in enumerate(raw_entries):
        items.append(
            _entry(
                path,
                name,
                child_rel,
                counts=index < COUNT_BUDGET,
                is_dir=is_dir,
                outside=outside,
                is_link=is_link,
            )
        )

    sorter = _SORTERS.get(sort, _SORTERS["name"])
    # 目录永远排在文件前面：书的目录里先列章节，再列页码上的东西
    items.sort(key=lambda item: (not item["is_dir"], *sorter(item)), reverse=desc)

    dirs = sum(1 for item in items if item["is_dir"])
    files = len(items) - dirs
    size_total = sum(item["size"] for item in items if not item["is_dir"])

    return {
        "path": rel,
        "abs_path": str(target),
        "entries": items,
        "counts": {"dirs": dirs, "files": files, "total": len(items)},
        "size_total": size_total,
        "truncated": truncated,
        "max_entries": MAX_ENTRIES,
    }


def measure(root: Path, rel: str) -> dict:
    """递归统计一个目录的总占用。

    这是**唯一**会走遍整棵子树的地方，所以给了明确的预算上限：
    超时或超量就停下来并如实标注 `truncated`，让用户知道这个数字是"至少"。
    只 stat、不打开文件。
    """
    start = resolve_in_root(root, rel)
    if not start.exists():
        raise PathNotAllowed("路径不存在")
    if not start.is_dir():
        st = start.stat()
        return {
            "bytes": st.st_size,
            "files": 1,
            "dirs": 0,
            "truncated": False,
            "errors": 0,
        }

    total = 0
    files = 0
    dirs = 0
    errors = 0
    visited = 0
    truncated = False
    deadline = time.monotonic() + SIZE_TIME_BUDGET

    stack = [start]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    visited += 1
                    if visited > SIZE_ENTRY_BUDGET or time.monotonic() > deadline:
                        truncated = True
                        break
                    try:
                        # 与包含性校验同一个口径：解析后仍在书库内才算数
                        if entry.is_dir(follow_symlinks=False):
                            dirs += 1
                            child = Path(entry.path)
                            if within(root, child.resolve(strict=False)):
                                stack.append(child)
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
        if truncated:
            break

    return {
        "bytes": total,
        "files": files,
        "dirs": dirs,
        "truncated": truncated,
        "errors": errors,
    }
