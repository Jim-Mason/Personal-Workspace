"""WebSocket 透传：让内网系统的 ws 页面经过跳板机也能连上。

双向转发，任一方向断开即结束会话。上游握手失败时以 1011 关闭并记录原因，
不把上游地址细节暴露给前端。
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import WebSocket

from app.core.config import settings
from app.core.errors import AppError
from app.models.entities import Card, CardEndpoint
from app.services.proxy_guard import validate_target
from app.services.proxy_service import _filter_cookie_header, _join_target

logger = logging.getLogger("app.proxy.ws")

try:  # websockets >= 13.1 的新实现
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - 兼容旧版本
    from websockets.client import connect as ws_connect  # type: ignore[no-redef]

# 允许作为关闭码透传（这两个是浏览器侧唯一合法的自定关闭码）
_FORWARDABLE_CLOSE_CODES = {1000, 1001}


def _to_ws_url(http_url: str) -> str:
    parts = urlsplit(http_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


async def _open_upstream(url: str, *, subprotocols: list[str], headers: dict[str, str]):
    """兼容不同 websockets 版本的握手参数名。"""
    common = {
        "subprotocols": subprotocols or None,
        "open_timeout": settings.proxy_connect_timeout_seconds,
        "ping_interval": None,  # 交由两端自行保活，避免代理层误杀
        "max_size": None,
        "close_timeout": 5,
    }
    try:
        return await ws_connect(url, additional_headers=headers or None, **common)
    except TypeError:  # websockets <= 13 使用 extra_headers
        return await ws_connect(url, extra_headers=headers or None, **common)


async def relay_websocket(
    websocket: WebSocket, *, card: Card | CardEndpoint, path: str
) -> None:
    """转发一条 WebSocket。

    `card` 实际可能是卡片、也可能是卡片下的一条环境地址 —— 只用到
    `slug` / `target_url` 两个属性，`CardEndpoint` 都提供（`url` 有同名别名），
    因此这里沿用旧参数名以免大范围改动，类型上放宽即可。
    """
    if not settings.proxy_enabled:
        await websocket.close(code=1013, reason="proxy disabled")
        return

    try:
        info = validate_target(card.target_url)
    except AppError as exc:
        logger.warning("WebSocket 目标校验失败 card=%s: %s", card.slug, exc.message)
        await websocket.accept()
        await websocket.close(code=1011, reason=exc.message[:120])
        return

    ws_url = _to_ws_url(_join_target(info.url, path, websocket.url.query))

    # 转发业务头，剥离门户自身 Cookie（与 HTTP 代理保持一致）
    forward_headers: dict[str, str] = {}
    raw_cookie = websocket.headers.get("cookie", "")
    filtered = _filter_cookie_header(raw_cookie, card.slug)
    if filtered:
        forward_headers["Cookie"] = filtered
    for name in ("origin", "user-agent", "accept-language"):
        value = websocket.headers.get(name)
        if value:
            forward_headers[name] = value

    subprotocols = [
        item.strip()
        for item in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if item.strip()
    ]

    try:
        upstream = await _open_upstream(ws_url, subprotocols=subprotocols, headers=forward_headers)
    except Exception as exc:  # noqa: BLE001 - 上游握手失败原因多样，统一处理
        logger.warning("WebSocket 上游握手失败 card=%s url=%s err=%s", card.slug, ws_url, exc)
        await websocket.accept()
        await websocket.close(code=1011, reason="上游 WebSocket 连接失败")
        return

    await websocket.accept(subprotocol=upstream.subprotocol)

    import asyncio

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                kind = message.get("type")
                if kind == "websocket.disconnect":
                    code = message.get("code", 1000)
                    await upstream.close(
                        code if code in _FORWARDABLE_CLOSE_CODES else 1000, "client disconnected"
                    )
                    return
                if kind != "websocket.receive":
                    continue
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("client→upstream 结束 card=%s err=%s", card.slug, exc)

    async def upstream_to_client() -> None:
        try:
            async for payload in upstream:
                if isinstance(payload, str):
                    await websocket.send_text(payload)
                else:
                    await websocket.send_bytes(payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("upstream→client 结束 card=%s err=%s", card.slug, exc)

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for task in done:
            exc = task.exception()
            if exc:
                logger.debug("WebSocket 任务异常 card=%s err=%s", card.slug, exc)
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await websocket.close(code=1000)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["relay_websocket"]
