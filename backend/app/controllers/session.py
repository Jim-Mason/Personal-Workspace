"""登录 / 登出 / 会话状态。

这三个接口本身是**免令牌**的 —— 它们就是进门的那道门。
`POST` 必须免，否则拿不到令牌就没法换会话；
`DELETE` 必须免，否则会话已经失效时连 Cookie 都清不掉。
`GET` 只回一个布尔值，泄露的东西少到可以忽略。

因为免令牌，匿名页面也能来 `POST` 试令牌，所以失败路径上挂了一个固定小延迟，
把「猜」的成本抬到不值得 —— 对正常登录则完全无感。
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config
from ..services import session as sessions

router = APIRouter(prefix="/api/session", tags=["session"])
log = logging.getLogger("localdeck.auth")

# 失败后的固定延迟。本地服务不需要指数退避那一套，
# 只要让「每个候选令牌一次请求」这条路走不通就够了。
_FAIL_DELAY_SECONDS = 0.4


class LoginBody(BaseModel):
    token: str = ""


def _expected_token(request: Request) -> str:
    """当前令牌从 app.state 拿，不重新读 data/.token —— 服务跑起来后它不会变，
    而每个请求都去戳一次磁盘是没必要的。"""
    return getattr(request.app.state, "token", "") or ""


def _clear_session(response: JSONResponse) -> JSONResponse:
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return response


@router.get("")
def status(request: Request) -> dict:
    sid = request.cookies.get(config.SESSION_COOKIE) or ""
    return {
        "authenticated": bool(sid) and sessions.verify(sid, _expected_token(request)),
        "ttl_days": config.SESSION_TTL_DAYS,
    }


@router.post("")
def login(request: Request, body: LoginBody) -> JSONResponse:
    expected = _expected_token(request)
    supplied = (body.token or "").strip()

    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        time.sleep(_FAIL_DELAY_SECONDS)
        log.warning("登录失败：提交的令牌与当前令牌不符（来源 %s）", request.client.host if request.client else "?")
        # 顺手清掉可能残留的旧 Cookie，免得页面一直拿着一条废会话重试
        return _clear_session(JSONResponse({"detail": "令牌不对"}, status_code=401))

    sid = sessions.create(expected, user_agent=request.headers.get("user-agent", ""))
    log.info(
        "登录成功，会话有效期 %d 天（当前活跃会话 %d 条）",
        config.SESSION_TTL_DAYS,
        sessions.count_active(),
    )

    response = JSONResponse({"authenticated": True, "ttl_days": config.SESSION_TTL_DAYS})
    response.set_cookie(
        config.SESSION_COOKIE,
        sid,
        max_age=config.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="strict",
        path="/",
        # 刻意不加 secure=True：本服务是 HTTP，加了浏览器会把 Cookie 直接丢掉，
        # 表现是「登录成功了但每次刷新又要重登」，很难查。
        # 之所以敢不加，是因为它只在 127.0.0.1 上跑，不存在被网络中间人截获的路径。
    )
    return response


@router.delete("")
def logout(request: Request) -> JSONResponse:
    sid = request.cookies.get(config.SESSION_COOKIE) or ""
    if sid:
        sessions.revoke(sid)
        log.info("已登出，会话已注销（剩余活跃会话 %d 条）", sessions.count_active())
    return _clear_session(JSONResponse({"authenticated": False}))
