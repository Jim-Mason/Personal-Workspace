"""浏览器会话：让「关掉页面再打开」不再需要令牌。

**令牌的定位没有变。** 它仍然是唯一的凭据，仍然只走 `X-LocalDeck-Token`
请求头，脚本调用和模块调用那条路径一个字都没动。这里只是给**浏览器**
补一层「进一次门、之后凭会话进出」：

    浏览器 --(令牌，只提交一次)--> 登录接口 --(会话 Cookie)--> 之后的每次访问

**为什么 Cookie 里装的不是令牌本身。**

Cookie 会被同源页面自动携带，所以一旦令牌进了 Cookie，任何能在这个浏览器里
执行的脚本都能拿到它（HttpOnly 能挡 JS 读取，但挡不住 CSRF —— 浏览器自己会带）。
装一个随机会话号就完全不同：它只在 `localdeck.db` 里能兑出权限，
泄露了也只能用在这台机器上，而且随时可以一键注销。

三重加固：

- 库里存的是**会话号的 sha256**，不是会话号本身 —— 拷走 db 也登不进来
- 会话绑定**令牌指纹** —— 重置令牌时所有旧会话立即失效
- Cookie 带 `HttpOnly` + `SameSite=Strict`，再叠上「只监听回环」+「Host 头校验」
  两道已有的防线，CSRF 没有立足点
"""

from __future__ import annotations

import hashlib
import secrets

from .. import config
from ..db import get_conn

# 令牌指纹加了前缀再哈希，免得跟会话号的哈希值撞进同一个空间。
_TOKEN_SALT = "localdeck-session-v1:"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_fingerprint(token: str) -> str:
    return _fingerprint(_TOKEN_SALT + token)[:32]


def create(token: str, *, user_agent: str = "") -> str:
    """签发一条会话，返回要写进 Cookie 的**会话号原文**。"""
    sid = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO session (id, token_fp, user_agent, expires_at)
            VALUES (?, ?, ?, datetime('now','localtime', ?))
            """,
            (
                _fingerprint(sid),
                token_fingerprint(token),
                (user_agent or "")[:200],
                f"+{config.SESSION_TTL_DAYS} days",
            ),
        )
    return sid


def verify(sid: str, token: str) -> bool:
    """会话是否仍然有效。

    过期判断交给 SQL 一次做完，不把过期时间捞回来再在 Python 里比 ——
    少一次「两边时钟/格式不一致」的机会。
    """
    if not sid:
        return False
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, token_fp FROM session
            WHERE id = ? AND expires_at > datetime('now','localtime')
            """,
            (_fingerprint(sid),),
        ).fetchone()
        if row is None:
            return False
        if not secrets.compare_digest(row["token_fp"], token_fingerprint(token)):
            # 令牌换过了，这条会话作废。顺手删掉，免得它继续占着位置。
            conn.execute("DELETE FROM session WHERE id = ?", (row["id"],))
            return False
        conn.execute(
            "UPDATE session SET last_seen_at = datetime('now','localtime') WHERE id = ?",
            (row["id"],),
        )
    return True


def revoke(sid: str) -> None:
    if not sid:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM session WHERE id = ?", (_fingerprint(sid),))


def purge_expired() -> int:
    """清掉过期会话。启动时跑一次就够了 —— 这是个单机服务，不值得上定时任务。"""
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM session WHERE expires_at <= datetime('now','localtime')"
        )
        return cursor.rowcount or 0


def count_active() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM session WHERE expires_at > datetime('now','localtime')"
        ).fetchone()
    return int(row["n"]) if row else 0
