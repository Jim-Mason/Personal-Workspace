"""模块的运行上下文：数据目录、数据库文件、机器标识。

### 为什么走环境变量，而不是 import 中台的包

模块要能**脱离中台源码单独存在**（这是这套模块机制的立身之本）：同一份代码
既要能被中台托管（native 同进程挂载），也要能在模块目录里自己跑起来调试。
一旦这里写成 `from app import config`，模块和中台的包名就绑死了 ——
换个目录、中台改个包名，模块立刻起不来。

所以中台在导入 native 模块**之前**，把这些只读的路径经环境变量交给模块
（见 `backend/app/services/modules/hub.py` 的 `_export_native_context`）；
读不到时退回模块自己的 `data/` 目录 —— 那条路正好是独立调试用的。

### 为什么数据还在中台的库里

模块一的数据（`resource` / `task` / `event` 三张表）是平台级资产：表结构由
中台的迁移文件定义，概览页也读它。所以这里**沿用同一个库文件**，只是自己
开连接。这不改变表结构、不搬数据，中台与模块看到的是同一份事实。
"""

from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent


def _env_path(name: str) -> Path | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


#: 中台托管时指向中台的 data/（由环境变量注入）；
#: 独立调试时是模块自己的 data/ —— 两者互不干扰。
DATA_DIR: Path = _env_path("LOCALDECK_DATA_DIR") or (MODULE_ROOT / "data")

#: 数据库文件。中台托管时与中台共用同一个库（表结构由中台的迁移定义）。
DB_PATH: Path = _env_path("LOCALDECK_DB_PATH") or (DATA_DIR / "localdeck.db")

_MACHINE_PATH: Path = DATA_DIR / ".machine"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def machine_id() -> str:
    """机器标识。多机场景下每条 task 都要带上它，否则分不清是谁跑的。

    中台托管时优先用注入的值；否则读与中台同一个 `.machine` 文件，
    保证两边写进 `task` 的值一致（对不上就没法归因了）。
    """
    injected = (os.environ.get("LOCALDECK_MACHINE_ID") or "").strip()
    if injected:
        return injected

    ensure_dirs()
    if _MACHINE_PATH.exists():
        value = _MACHINE_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "unknown"
    value = f"{hostname}-{secrets.token_hex(3)}"
    _MACHINE_PATH.write_text(value, encoding="utf-8")
    return value
