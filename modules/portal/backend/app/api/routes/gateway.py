"""网关端点：`/gw/{slug}/{path}` 内网反向代理入口。

访问这些路由需要先登录（复用门户的 HttpOnly Cookie 认证），
因此网关本身不会再向公网暴露任何内网地址。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response, WebSocket
from fastapi.responses import Response as PlainResponse

from app.api.deps import DbSession, local_trust_active
from app.core.config import settings
from app.core.errors import NotFoundError, UnauthorizedError
from app.core.security import ACCESS_COOKIE, decode_token
from app.db.session import SessionLocal
from app.models.entities import Card, CardEndpoint
from app.repositories.repositories import (
    CardEndpointRepository,
    CardRepository,
    UserRepository,
)
from app.services.proxy_service import error_page, gateway

logger = logging.getLogger("app.api.gateway")

router = APIRouter(tags=["gateway"])

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def authenticate_request(request: Request) -> None:
    """网关鉴权：与 API 一致，走 HttpOnly Cookie / Bearer。

    main.py 的兜底转发路由也会调用它 —— Referer 是客户端可控的，
    没有这道校验，任何人伪造一个 Referer 就能白嫖内网访问。
    """
    # 免登录模式（挂在中台后面时）：网关这层不需要「当前用户」，
    # 只要确认请求确实来自中台代理即可。刻意复用 API 侧同一个判定函数，
    # 免得两处口径不一致、留下一条只在网关上成立的暗路。
    if local_trust_active(request):
        return

    token = request.cookies.get(ACCESS_COOKIE, "")
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        raise UnauthorizedError("请先在门户登录")
    decode_token(token, expected_type="access")


def resolve_target(db, slug: str) -> tuple[Card | CardEndpoint | None, Card | None]:  # noqa: ANN001
    """按入口标识找到转发目标。

    标识在**同一个命名空间**里有两个来源：卡片本身（`cards.slug`）与卡片下的
    一条环境地址（`card_endpoints.slug`）。先查卡片再查环境地址，与
    `CardService._unique_slug` 的命名空间约定一致。

    返回 `(转发目标, 归属卡片)`：转发目标可能是环境地址，但"是否停用"永远看
    它所属的卡片 —— 停用整张卡片就该把它的所有环境入口一起关掉。

    转发逻辑只依赖 `slug` / `open_mode` / `target_url` 三个属性
    （`CardEndpoint` 有同名 `target_url` 别名），所以换成一个环境地址之后，
    路径改写、Cookie 过滤、TLS 策略全都是现成的，不需要另写一套。
    """
    card = CardRepository(db).get_by_slug(slug)
    if card is not None:
        return card, card

    endpoint = CardEndpointRepository(db).get_by_slug(slug)
    if endpoint is None:
        return None, None
    return endpoint, endpoint.card


@router.api_route("/gw/{slug}", methods=_PROXY_METHODS, include_in_schema=False)
async def gateway_slash_redirect(slug: str, request: Request) -> PlainResponse:
    """不带尾斜杠时补一个 308，保证相对路径与 <base> 生效。"""
    authenticate_request(request)
    suffix = f"?{request.url.query}" if request.url.query else ""
    root = (settings.root_path or "").rstrip("/")
    return PlainResponse(status_code=308, headers={"Location": f"{root}/gw/{slug}/{suffix}"})


@router.api_route("/gw/{slug}/{path:path}", methods=_PROXY_METHODS, include_in_schema=False)
async def gateway_http(slug: str, path: str, request: Request, db: DbSession) -> Response:
    authenticate_request(request)

    if not settings.proxy_enabled:
        return error_page(title="反向代理已关闭", detail="服务端配置 PROXY_ENABLED=false，已停用网关。")

    target, card = resolve_target(db, slug)
    if target is None or card is None:
        return error_page(
            title="入口不存在",
            detail=f"未找到标识为 {slug} 的卡片或环境地址，可能是配置已变更。",
        )
    if not card.enabled:
        return error_page(title="卡片已停用", detail=f"卡片 {card.title} 当前处于停用状态。")

    return await gateway.handle_http(request, target, path)


@router.websocket("/gw/{slug}/{path:path}")
async def gateway_websocket(websocket: WebSocket, slug: str, path: str) -> None:
    # 免登录模式下浏览器手里没有会话 Cookie，若这里仍按 Cookie 校验，
    # 被代理页面里的 WebSocket 会被无声掐断（1008），现象是「页面能打开、
    # 但实时部分永远不更新」，极难归因。所以走同一个判定函数。
    # （WebSocket 对象与 Request 在 headers / client 这两项上形状一致。）
    trusted = local_trust_active(websocket)  # type: ignore[arg-type]

    if not trusted:
        # WebSocket 没有 Bearer 头的常规使用场景，这里用 Cookie 认证
        token = websocket.cookies.get(ACCESS_COOKIE, "")
        if not token:
            await websocket.accept()
            await websocket.close(code=1008, reason="unauthorized")
            return

        try:
            payload = decode_token(token, expected_type="access")
        except Exception:  # noqa: BLE001
            await websocket.accept()
            await websocket.close(code=1008, reason="unauthorized")
            return

        with SessionLocal() as db:
            user = UserRepository(db).get_by_uid(str(payload.get("sub", "")))
            if user is None or not user.is_active:
                await websocket.accept()
                await websocket.close(code=1008, reason="unauthorized")
                return
            # 环境地址需要在这里把归属卡片一并取出来：会话关闭后就取不到关系字段了
            target, card = resolve_target(db, slug)
    else:
        with SessionLocal() as db:
            target, card = resolve_target(db, slug)

    if target is None or card is None or not card.enabled:
        await websocket.accept()
        await websocket.close(code=1011, reason="card unavailable")
        return

    await gateway.handle_websocket(websocket, target, path)


__all__ = ["router", "NotFoundError", "resolve_target"]
