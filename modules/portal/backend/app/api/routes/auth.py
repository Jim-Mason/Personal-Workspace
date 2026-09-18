"""认证相关端点：登录 / 刷新 / 登出 / 当前用户 / 改密。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.api.deps import ClientIp, CurrentUser, DbSession
from app.core.errors import UnauthorizedError
from app.core.security import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    decode_token,
    issue_auth_cookies,
)
from app.repositories.repositories import UserRepository
from app.schemas.models import (
    LoginRequest,
    MessageOut,
    PasswordChangeRequest,
    SessionOut,
    UserOut,
)
from app.services.auth_service import AuthService

logger = logging.getLogger("app.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=SessionOut, summary="账号密码登录")
async def login(
    payload: LoginRequest,
    response: Response,
    db: DbSession,
    ip: ClientIp,
) -> SessionOut:
    user = AuthService(db).authenticate(payload.username, payload.password, ip=ip)
    expires = issue_auth_cookies(
        response, user_id=user.uid, username=user.username, role=user.role
    )
    logger.info("用户登录成功 username=%s ip=%s", user.username, ip)
    return SessionOut(user=UserOut.model_validate(user), **expires)


@router.post("/refresh", response_model=SessionOut, summary="用刷新令牌换取新的访问令牌")
async def refresh(request: Request, response: Response, db: DbSession) -> SessionOut:
    token = request.cookies.get(REFRESH_COOKIE, "")
    if not token:
        raise UnauthorizedError("缺少刷新令牌，请重新登录")

    payload = decode_token(token, expected_type="refresh")
    user = UserRepository(db).get_by_uid(str(payload.get("sub", "")))
    if user is None or not user.is_active:
        clear_auth_cookies(response)
        raise UnauthorizedError("账号不可用，请重新登录", code="user_unavailable")

    expires = issue_auth_cookies(
        response, user_id=user.uid, username=user.username, role=user.role
    )
    return SessionOut(user=UserOut.model_validate(user), **expires)


@router.post("/logout", response_model=MessageOut, summary="登出")
async def logout(response: Response) -> MessageOut:
    clear_auth_cookies(response)
    return MessageOut(message="已登出")


@router.get("/me", response_model=UserOut, summary="获取当前登录用户")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/password", response_model=MessageOut, summary="修改本人密码")
async def change_password(
    payload: PasswordChangeRequest,
    user: CurrentUser,
    db: DbSession,
    ip: ClientIp,
) -> MessageOut:
    AuthService(db).change_password(
        user, payload.old_password, payload.new_password, ip=ip
    )
    return MessageOut(message="密码已更新")
