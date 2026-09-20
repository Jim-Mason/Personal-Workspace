"""全局配置。所有路径、端口、令牌都在这里收口，别在业务代码里散落。"""

from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path

# 对外品牌名只是「出厂默认值」，用户在界面里随时可改（存 meta 表）。
# 真正的显示名以 branding 配置为准，这里这份只在配置为空时兜底。
APP_NAME = "Personal Workspace"
APP_VERSION = "0.6.0"

# 内部标识，不随品牌改名而变：
# 令牌请求头 X-LocalDeck-Token、数据库文件名、目录名都用它。
# 这三样一旦跟风改名，已有用户的 data/localdeck.db 和浏览器里的书签就全废了。
APP_ID = "LocalDeck"

# 只监听回环地址。这是硬约束，任何情况下都不得改成 0.0.0.0。
HOST = "127.0.0.1"
PORT = int(os.environ.get("LOCALDECK_PORT", "8731"))

_APP_DIR = Path(__file__).resolve().parent          # backend/app
BACKEND_DIR = _APP_DIR.parent                        # backend
PROJECT_DIR = BACKEND_DIR.parent                      # 项目根
DATA_DIR = Path(os.environ.get("LOCALDECK_DATA_DIR") or (PROJECT_DIR / "data"))
WEB_DIR = PROJECT_DIR / "web"
MIGRATIONS_DIR = _APP_DIR / "migrations"

# 模块目录。模块在这里各自成家，中台只读它的 module.py 与静态产物。
MODULES_DIR = PROJECT_DIR / "modules"
# 模块进程的 stdout/stderr。放中台自己的 data/ 下，不往模块目录里写 ——
# 「各模块数据落在自己的 data/ 下」这条对写入方向同样成立。
MODULE_LOG_DIR = DATA_DIR / "module-logs"
# 中台提供的启动壳。模块不允许自己决定监听地址，所以进程都由壳来起。
RUNNERS_DIR = _APP_DIR / "services" / "modules" / "runners"

DB_PATH = DATA_DIR / "localdeck.db"
TOKEN_PATH = DATA_DIR / ".token"
MACHINE_PATH = DATA_DIR / ".machine"

# 运行日志。刻意放在 data/ 下 —— 「备份 = 拷一个 data 目录」这条约定
# 不能因为多了日志就破功。日志也会跟着一起被拷走，这是好事：
# 出问题时手上正好有现场。体积由轮转兜住，见 logging_setup.py。
LOG_DIR = DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "localdeck.log"

# 浏览器会话（Cookie）的有效期，单位天。只影响「页面关了再开要不要重输令牌」，
# 不影响令牌本身 —— 令牌没有过期时间，它只在被删掉时失效。
SESSION_TTL_DAYS = 30
# 会话 Cookie 的名字。定义在这里是因为有两处要用它：
# 安全层（读它判断能不能放行）和会话服务（发它、注销它）。
# 各写一份是这类常量最容易出的错 —— 改了名字只改一处，表现是「登录成功但立刻又退出来」。
SESSION_COOKIE = "pw_session"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_or_create_token() -> str:
    """访问令牌：首次生成后持久化。

    之所以不用「每次启动都换」，是因为那样浏览器书签每开一次就失效，
    自己用会非常烦。文件在 data/ 下，本机磁盘被读到时令牌本身已无意义。
    需要重置时删掉 data/.token 即可。
    """
    ensure_dirs()
    env_token = os.environ.get("LOCALDECK_TOKEN")
    if env_token:
        return env_token
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def get_or_create_machine_id() -> str:
    """机器标识。多机场景下每条 task 都要带上它，否则分不清是谁跑的。"""
    ensure_dirs()
    if MACHINE_PATH.exists():
        value = MACHINE_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "unknown"
    value = f"{hostname}-{secrets.token_hex(3)}"
    MACHINE_PATH.write_text(value, encoding="utf-8")
    return value
