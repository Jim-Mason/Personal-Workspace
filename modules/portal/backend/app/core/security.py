"""密码哈希与 JWT 签发/校验。

- 密码：PBKDF2-HMAC-SHA256（标准库实现，带随机 salt 与迭代次数，可平滑升级参数）
- 令牌：HS256 JWT，Access 短时效 + Refresh 长时效，均通过 HttpOnly Cookie 下发
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt

from app.core.config import settings
from app.core.errors import UnauthorizedError

_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 260_000
_TOKEN_ALGORITHM = "HS256"

TokenType = Literal["access", "refresh"]

ACCESS_COOKIE = "dtb_access"
REFRESH_COOKIE = "dtb_refresh"


# ----------------------------------------------------------------------
# 密码
# ----------------------------------------------------------------------
def hash_password(password: str) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "$".join(
        [
            _PBKDF2_ALGO,
            str(_PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    try:
        algo, iterations, _, _ = stored.split("$")
    except ValueError:
        return True
    return algo != _PBKDF2_ALGO or int(iterations) < _PBKDF2_ITERATIONS


# ----------------------------------------------------------------------
# JWT
# ----------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_token(
    *,
    subject: str,
    token_type: TokenType,
    extra: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> tuple[str, datetime]:
    if expires_delta is None:
        expires_delta = (
            timedelta(minutes=settings.access_token_expire_minutes)
            if token_type == "access"
            else timedelta(days=settings.refresh_token_expire_days)
        )
    issued_at = _now()
    expires_at = issued_at + expires_delta
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_hex(12),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.secret_key, algorithm=_TOKEN_ALGORITHM)
    return token, expires_at


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[_TOKEN_ALGORITHM],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("登录已过期，请重新登录", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("凭证无效", code="token_invalid") from exc

    if expected_type is not None and payload.get("typ") != expected_type:
        raise UnauthorizedError("凭证类型不匹配", code="token_type_mismatch")
    return payload


# ----------------------------------------------------------------------
# Cookie 下发 / 清除
# ----------------------------------------------------------------------
def _cookie_kwargs(max_age: int) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "max_age": max_age,
        "path": "/",
    }
    if settings.cookie_domain:
        kwargs["domain"] = settings.cookie_domain
    return kwargs


def issue_auth_cookies(response: Any, *, user_id: str, username: str, role: str) -> dict[str, Any]:
    """写入 access/refresh Cookie，返回给前端展示的令牌元信息（不含令牌本体）。"""
    access_token, access_exp = create_token(
        subject=user_id, token_type="access", extra={"username": username, "role": role}
    )
    refresh_token, refresh_exp = create_token(
        subject=user_id, token_type="refresh", extra={"username": username}
    )

    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        **_cookie_kwargs(int((access_exp - _now()).total_seconds())),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        **_cookie_kwargs(int((refresh_exp - _now()).total_seconds())),
    )
    return {
        "access_expires_at": access_exp.isoformat(),
        "refresh_expires_at": refresh_exp.isoformat(),
    }


def clear_auth_cookies(response: Any) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(
            name,
            path="/",
            domain=settings.cookie_domain or None,
            httponly=True,
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,
        )
