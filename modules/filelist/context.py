"""模块的运行上下文：自己的数据目录。

与 `modules/inventory/context.py` 的差别值得说清楚，因为很容易搞混：

| | `LOCALDECK_DATA_DIR` | `LOCALDECK_MODULE_DATA_DIR` |
|---|---|---|
| 指向 | 中台的 `data/` | 本模块目录下的 `data/` |
| 装什么 | 共享库 `localdeck.db`（平台级的表） | 本模块自己的配置 |
| 谁该用 | 需要写 `resource`/`task`/`event` 的模块（如模块一） | **本模块**以及任何只要存自己东西的模块 |

本模块不碰中台的库 —— 它只有一份「书库根目录」配置，属于自己，放自己家里。
中台侧对应 `hub._export_native_context()`。

读不到环境变量时（独立调试：直接在模块目录里跑）退回模块自己的 `data/`，
两个场景互不干扰。
"""

from __future__ import annotations

import os
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


#: 本模块自己的数据目录。
DATA_DIR: Path = _env_path("LOCALDECK_MODULE_DATA_DIR") or (MODULE_ROOT / "data")

#: 书库配置（用户登记的根目录）。
LIBRARIES_FILE: Path = DATA_DIR / "libraries.json"

#: 模块自带页面的位置。
WEB_DIR: Path = MODULE_ROOT / "web"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
