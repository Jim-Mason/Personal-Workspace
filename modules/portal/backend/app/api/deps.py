"""FastAPI 依赖：数据库会话、当前用户、权限、客户端 IP。"""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import ACCESS_COOKIE, decode_token
from app.db.session import get_db
from app.models.entities import User
from app.repositories.repositories import UserRepository

DbSession = Annotated[Session, Depends(get_db)]


def client_ip(request: Request) -> str:
    """取真实客户端 IP：优先 X-Forwarded-For 的第一跳（反向代理场景）。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else ""


ClientIp = Annotated[str, Depends(client_ip)]


def _extract_token(request: Request) -> str:
    """优先 HttpOnly Cookie；同时兼容 Authorization: Bearer（便于脚本调用）。"""
    cookie_token = request.cookies.get(ACCESS_COOKIE, "")
    if cookie_token:
        return cookie_token
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def local_trust_active(request: Request) -> bool:
    """这次请求能不能免登录（挂在中台后面时的本地自用场景）。

    三个条件**必须同时成立**，缺一个就退回正常登录：

    1. 开关打开（`TRUST_LOCAL_PROXY`）。默认关闭，只有中台启动本模块时注入。
    2. 来源是回环地址。本模块只绑 127.0.0.1，这条是为了挡住「被别的机器
       代理转发进来」这种情况。
    3. 请求头里的 `X-LocalDeck-Module-Ticket` 与中台启动时交给我们的票据
       一致。**这条是真正的凭据** —— 只认回环不够，本机任何进程都能连
       127.0.0.1；`X-Forwarded-Prefix` 之类的头也都能伪造。票据是每次运行
       新生成的随机串，只在中台与模块之间传递。

    独立运行（不经中台）时环境变量不存在 → 条件 3 永远不成立 → 只能正常登录。
    这道兜底是刻意的：免登录的正当性完全来自上游那道边界，边界不在就不能免。
    """
    if not settings.trust_local_proxy:
        return False
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK_HOSTS:
        return False
    expected = os.environ.get("LOCALDECK_MODULE_TICKET", "")
    if not expected:
        return False
    supplied = request.headers.get("x-localdeck-module-ticket", "")
    if not supplied:
        return False
    return secrets.compare_digest(supplied, expected)


def _trusted_user(db: Session) -> User | None:
    """免登录时以谁的身份行事：优先配置里的用户名，否则取用户名最小的管理员。"""
    repo = UserRepository(db)
    username = (settings.trust_username or "").strip()
    if username:
        user = repo.get_by_username(username)
        if user is not None and user.is_active:
            return user
    candidates = repo.list_all()
    for user in candidates:
        if getattr(user, "is_admin", False) and user.is_active:
            return user
    return None


def get_current_user(request: Request, db: DbSession) -> User:
    if local_trust_active(request):
        user = _trusted_user(db)
        if user is not None:
            return user
        # 走到这里说明开关开着但库里没有可用的管理员 —— 不静默放行，
        # 而是退回正常登录，让人能看出「不是我认错人，是库里没有这个人」。
        raise UnauthorizedError(
            "已开启本地免登录，但库里找不到配置的管理员账号，请先登录并检查账号",
            code="trust_user_missing",
        )

    token = _extract_token(request)
    if not token:
        raise UnauthorizedError("请先登录")

    payload = decode_token(token, expected_type="access")
    user = UserRepository(db).get_by_uid(str(payload.get("sub", "")))
    if user is None:
        raise UnauthorizedError("账号不存在或已被删除", code="user_not_found")
    if not user.is_active:
        raise ForbiddenError("账号已被禁用", code="user_disabled")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise ForbiddenError("该操作需要管理员权限")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


__all__ = [
    "DbSession",
    "CurrentUser",
    "AdminUser",
    "ClientIp",
    "get_current_user",
    "client_ip",
    "local_trust_active",
]
