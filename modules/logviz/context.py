"""模块的运行上下文：自己的数据目录与库。

与 `modules/filelist/context.py` 是同一个模板，只多了一样东西 —— 缓存库。
区别值得说清楚，因为很容易搞混：

| | `LOCALDECK_DATA_DIR` | `LOCALDECK_MODULE_DATA_DIR` |
|---|---|---|
| 指向 | 中台的 `data/` | 本模块目录下的 `data/` |
| 装什么 | 共享库 `localdeck.db`（平台级的表） | 本模块自己的配置与缓存 |
| 谁该用 | 需要写 `resource`/`task`/`event` 的模块（如模块一） | **本模块**：日志根目录配置 + 分析缓存 |

本模块不碰中台的库 —— 日志根目录名单和分析缓存都属于自己，放自己家里。
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

#: 用户登记的「日志根目录」名单。
ROOTS_FILE: Path = DATA_DIR / "roots.json"

#: 分析缓存库。**只装缓存与索引，不装用户的日志内容** ——
#: 缓存里存的是聚合结果与少量样本行，不整份拷贝日志。
CACHE_DB: Path = DATA_DIR / "logviz.db"

#: 模块自带页面的位置。
WEB_DIR: Path = MODULE_ROOT / "web"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
