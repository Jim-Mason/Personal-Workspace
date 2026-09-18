"""路径包含性校验。

这是整个模块的安全阀，从 `modules/filelist/lister.py` 复刻而来。

### 为什么要复刻一份，而不是 import 模块四的

中台的铁律：**native 模块各自是独立包**（`localdeck_modules.<id>`），
模块之间不互相 import —— 一旦互相依赖，"装单个模块即用"就不成立了
（想装 logviz 还得先有 filelist）。所以这三段校验在这里有意识地重复一份。

真要改动这里的任何一条判据，**两个模块都要改**，否则会出现
"一边拒了、另一边没拒"的裂缝。

### ⛔ 三条不许放宽的约束

1. 拒 `..`、拒绝对路径、拒盘符 —— 只接受相对路径
2. 解析完必须 `resolve()` **解开符号链接**再比对一次 ——
   少了这一步，根目录里一个指向 `C:\\Windows` 的链接就能把整条路径带出去
3. 越界的条目**连 stat 都不做** —— 哪怕只是读一个大小，那也是"读了根目录外的东西"

### 为什么判断用 `normcase` 而不是直接比字符串

Windows 路径大小写不敏感（`C:\\Data` 与 `c:\\data` 是同一个地方）。
不做归一化就会出现"明明在根里却被判成越界"，或者更糟：绕过检查。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

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
        raise PathNotAllowed(f"路径跑到登记的日志目录之外了：{rel!r}")
    return resolved


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


def list_logs(root: Path, rel: str, *, search: str = "", limit: int = 800) -> dict:
    """列一层目录，并且**只报出看起来像日志的文件**。

    与模块四的差别：这里不追求"把整棵目录树画出来"，而是要回答
    "这里面有哪些日志可以分析"。所以：

    - 目录照列（要能往下走）
    - 文件按「像不像日志」打个分数报上去，界面上排前面
    - 不递归、不预取 —— 一层一层点

    同样只 stat、不打开。
    """
    target = resolve_in_root(root, rel)
    if not target.is_dir():
        raise PathNotAllowed("这个路径不是一个目录")

    needle = (search or "").strip().lower()
    dirs: list[dict] = []
    files: list[dict] = []
    truncated = False

    try:
        with os.scandir(target) as it:
            for entry in it:
                if len(dirs) + len(files) >= limit:
                    truncated = True
                    break
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                resolved = Path(entry.path).resolve(strict=False)
                outside = not within(root, resolved)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    dirs.append(
                        {
                            "name": entry.name,
                            "path": child_rel,
                            "is_dir": True,
                            "outside": outside,
                            "is_link": _is_reparse(entry),
                            "size": 0,
                            "mtime": 0.0,
                            "score": 0,
                            "hint": "",
                        }
                    )
                    continue
                if needle and needle not in entry.name.lower():
                    continue
                if outside:
                    # 指向根目录之外的链接：**连 stat 都不做**
                    files.append(
                        {
                            "name": entry.name,
                            "path": child_rel,
                            "is_dir": False,
                            "outside": True,
                            "is_link": True,
                            "size": 0,
                            "mtime": 0.0,
                            "score": 0,
                            "hint": "外部链接",
                        }
                    )
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                score, hint = _log_likeness(entry.name, st.st_size)
                files.append(
                    {
                        "name": entry.name,
                        "path": child_rel,
                        "is_dir": False,
                        "outside": False,
                        "is_link": _is_reparse(entry),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "score": score,
                        "hint": hint,
                    }
                )
    except PermissionError as exc:
        raise PathNotAllowed("没有权限读取这个目录") from exc
    except OSError as exc:
        raise PathNotAllowed(f"无法读取这个目录：{exc}") from exc

    dirs.sort(key=lambda item: item["name"].lower())
    # 像日志的排前面：用户点进来十有八九是要分析，不是要按字母顺序欣赏文件名
    files.sort(key=lambda item: (-item["score"], item["name"].lower()))

    return {
        "path": rel,
        "abs_path": str(target),
        "dirs": dirs,
        "files": files,
        "counts": {"dirs": len(dirs), "files": len(files)},
        "truncated": truncated,
    }


#: 明显是日志的名字，给最高分
_STRONG = (
    ".log",
    "access",
    "error",
    "slow",
    "catalina",
    "localhost",
    "stdout",
    "stderr",
    "audit",
    "trace",
    "journal",
)
#: 可能是日志，但不一定
_WEAK = (".txt", ".out", ".err", "log")
#: 基本不可能是行式日志的
_SKIP_EXT = {
    ".zip", ".gz", ".7z", ".rar", ".tar", ".bz2", ".xz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
    ".exe", ".dll", ".so", ".jar", ".war", ".class", ".pyc",
    ".mp4", ".mp3", ".avi", ".mkv", ".pdf", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".db", ".sqlite", ".bin",
}


def _log_likeness(name: str, size: int) -> tuple[int, str]:
    """给一个文件名打个「像不像日志」的分。

    刻意做得保守：宁可漏判成"不确定"，也不要把 `package.json`
    排到 `access.log` 前面去。
    """
    lower = name.lower()
    ext = ""
    if "." in lower:
        ext = "." + lower.rsplit(".", 1)[-1]
    if ext in _SKIP_EXT:
        return -10, "多半不是文本日志"
    if size == 0:
        return -5, "空文件"
    for token in _STRONG:
        if token in lower:
            return 100, "日志"
    for token in _WEAK:
        if token in lower:
            return 40, "可能是日志"
    return 0, ""
