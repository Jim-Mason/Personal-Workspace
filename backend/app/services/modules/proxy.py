"""反向代理：把 `/opsgen/...` 转成 `http://127.0.0.1:8311/...`。

### 为什么这个代理要「看懂」内容，而不只是转发字节

如果模块住在根路径，转发就够了。但模块被挂在子路径下，它自己不知道，
于是它吐出来的每一个根路径都会打到中台自己脸上 —— `<link href="/static/...">`
会去中台找 CSS，`fetch('/api/templates')` 会去调中台的接口。所以必须在
返回路径上把「根路径」补成「模块路径」，同时把 `Location` 和 `Set-Cookie`
一起收拢回来。

### 凭据隔离（本文件最重要的一条）

浏览器要能加载模块页面里的 CSS/JS/图片 —— 那些请求**带不上自定义请求头**。
常规解法是把中台令牌拼进 URL，但那样第三方模块的 JavaScript 就能从
`location.search` 里捡到中台令牌，反过来调中台的 `/api/*`（能读本机全部
软件清单、能改外观配置）。

所以这里不用中台令牌，改用**模块级票据**：

- 票据只对 `/opsgen/*` 有效，偷去也够不到中台
- 通过 `?_t=` 只用一次，用来换一个 `HttpOnly` 的 Cookie，之后自动带上
- **中台令牌本身只走请求头**，不落进任何 URL、不进 Cookie

### 两处刻意阻断

1. `socket.io` / WebSocket 通道。默认不放行 —— 那条通道上挂着
   「在线执行脚本」，本质是任意命令执行面。
2. 外部 CDN 资源。默认剥掉，对应「零网络上传、无遥测」。
   两处都可以在 `module.py` 里单独打开，且打开时会在模块日志里留痕。
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .registry import ModuleRegistry, ModuleRuntime
from .rewrite import (
    inject_shim,
    is_rewritable,
    rewrite_css,
    rewrite_html,
    rewrite_location_header,
    rewrite_set_cookie,
    safe_subpath,
    strip_external_subresources,
)
from .spec import ModuleSpec

# 这些头是「逐跳」的，代理不该转发（RFC 9110 §7.6.1）
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# 请求侧要被中台拿掉的头
_REQUEST_DROP = _HOP_BY_HOP | {
    "host",             # 重新构造，见 _build_headers
    "accept-encoding",  # 换成 identity，返回体我们要改
    "content-length",   # httpx 自己算
    "x-localdeck-token",  # 中台令牌绝不进模块
    "cookie",           # 模块票据与平台 Cookie 都不流向模块
}

# 响应侧要拿掉的头（set-cookie 逐条重发，所以也从映射里剔除）
_RESPONSE_DROP = _HOP_BY_HOP | {"content-encoding", "content-length", "set-cookie"}

# 中台自己下发的 Cookie 前缀。**无论模块有没有被放行 Cookie，这些都挡掉** ——
# 平台凭据不进第三方进程，这条不让步。
_PLATFORM_COOKIE_PREFIXES = ("pw_mod_", "localdeck")


def _forwardable_cookies(request: Request) -> str:
    """挑出可以转给模块的 Cookie（仅在 allow_module_cookies 为真时调用）。

    规则很直白：模块自己的会话留下，中台的一律挡掉 ——
    「放行模块的会话」不等于「放行平台的凭据」，这两件事必须分开。
    """
    raw = request.headers.get("cookie", "")
    if not raw:
        return ""
    kept: list[str] = []
    for piece in raw.split(";"):
        piece = piece.strip()
        name, sep, _ = piece.partition("=")
        if not sep:
            continue
        if name.strip().lower().startswith(_PLATFORM_COOKIE_PREFIXES):
            continue
        kept.append(piece)
    return "; ".join(kept)


def _cookie_names(header_values: list[str]) -> set[str]:
    """把若干条 Cookie 头拆成「名字集合」。

    只比名字、不比值：值的编码可能被 HTTP 客户端规整，比名字既够用又不会
    误伤。而越权/串模块这类事故一定表现为**多出一个名字**（比如罐里那份
    `dtb_access`），所以比名字正好能抓住它。
    """
    names: set[str] = set()
    for value in header_values:
        for piece in value.split(";"):
            name, sep, _ = piece.strip().partition("=")
            if sep and name.strip():
                names.add(name.strip().lower())
    return names


def _disable_cookie_jar(client: httpx.AsyncClient) -> None:
    """把 httpx 自带的 Cookie 罐变成「只读为空」—— 中台是转发者，不是客户端。

    为什么必须做这件事（这是本文件里最不显眼、但后果最重的一处）：

    `httpx` 默认给每个 Client 配一个 Cookie 罐，并在**每一次**响应到达时
    执行 `self.cookies.extract_cookies(response)`，之后的请求再由
    `_merge_cookies()` 把罐里的 Cookie 合并进出站请求。于是中台这个
    **长期存活、被所有模块共用**的客户端就变成了一个有状态的会话容器：

    1. **越权**。用户在 `/portal/` 登录一次，模块返回的 `dtb_access`
       就被存进罐里。此后任何**没有带 Cookie** 的请求（哪怕只拿着中台
       票据）转发过来时，罐里的会话都会被自动贴上 —— 等于谁点开
       `/portal/` 谁就是上次登录的那个人。实测：零 Cookie 访问
       `/portal/api/auth/me` 返回 200 + 管理员身份。
    2. **串模块**。Cookie 的作用域只看域名、**不看端口**。中台所有模块
       都在 `127.0.0.1` 上（8732/8733/… 只是端口不同），所以 A 模块的
       会话会被原样送给 B 模块。第三方模块拿到别人的会话，凭据隔离就
       成了摆设。

    修法上不动 httpx 的公开接口：`Client.cookies` 的 setter 会把赋进去的
    对象重新包成普通 `Cookies`，改了也没用；而 `_send_single_request` 调用
    的是 `self.cookies.extract_cookies(...)`，所以只要在**罐实例**上把这个
    方法置空，写入通路就断了。`set_cookie` 一并置空，防止别的代码路径
    往罐里塞东西。

    出站请求该带哪些 Cookie，只由 `_forwardable_cookies()` 按当前这一次
    请求现算 —— 见 `_build_headers()`。另有一道不变量校验兜底，
    见 `_forward()`。
    """
    jar = client.cookies
    jar.extract_cookies = lambda *args, **kwargs: None  # type: ignore[method-assign]
    jar.set_cookie = lambda *args, **kwargs: None  # type: ignore[method-assign]
    # 保险丝：万一将来 httpx 换了写入通路，这里会立刻在启动阶段暴露，
    # 而不是等到某天有人发现自己的会话被送给了别的模块。
    if len(jar) != 0:
        jar.clear()


# 超过这个体积就不做内容改写，直接流式转发（下载 ZIP 之类走这条路）
_MAX_REWRITE_BYTES = 4 * 1024 * 1024


class ModuleProxy:
    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry
        # 回环地址 + 单用户，连接池不需要开大；
        # 但超时必须给：模块假死时不能让中台的请求一直挂着。
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        # 这个客户端被所有模块共用且活得比任何一次请求都长，绝不能让它
        # 记住任何模块的会话 Cookie —— 理由见 _disable_cookie_jar()。
        _disable_cookie_jar(self._client)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ 入口
    async def handle(self, request: Request, module_id: str, subpath: str) -> Response:
        runtime = self._registry.runtime(module_id)
        if runtime is None:
            return JSONResponse({"detail": f"没有这个模块：{module_id}"}, status_code=404)
        spec = runtime.spec

        if not spec.enabled:
            return self._deny(
                runtime,
                "模块已被禁用",
                f"{spec.name} 在 module.py 里是 enabled=False，中台不为它提供代理。",
                status_code=503,
            )
        if spec.kind in {"subprocess_proxy", "external"} and runtime.state != "running":
            detail = runtime.error or "模块进程没有在运行"
            return self._deny(
                runtime,
                "模块未就绪",
                f"{detail} 到中台的「模块」面板点启动，或看它的日志尾部。",
                status_code=503,
            )

        safe = safe_subpath(subpath)
        if safe == "" and subpath.strip("/\\"):
            return self._deny(
                runtime, "路径不合法", f"拒绝转发可疑路径：{subpath!r}", status_code=400
            )

        if spec.kind in {"native", "static"}:
            # native / static 模块**没有内部端口** —— 它们的路由已经直接挂进中台
            # 了（见 hub.mount_native / mount_static）。请求能走到这里，说明
            # 没有任何一条模块路由匹配，也就是说「这个路径不存在」。
            #
            # 绝不能顺手"转发"一下：目标端口是 0，转发必然失败，最后丢给用户的
            # 是一句「连不上模块进程、端口 0 拒绝连接」—— 而它压根不该有端口，
            # 这句话只会把人往错的方向带。
            return JSONResponse(
                {
                    "detail": (
                        f"{spec.name} 是"
                        + ("同进程挂载" if spec.kind == "native" else "托管的静态产物")
                        + f"的模块，没有 {request.url.path} 这个路径"
                    ),
                    "path": request.url.path,
                    "module": spec.id,
                },
                status_code=404,
            )

        # ---- 阻断 1：实时通道
        if not spec.allow_realtime and self._is_realtime_path(safe):
            return self._deny(
                runtime,
                "实时通道未放行",
                "中台默认不放行模块的 socket.io / WebSocket 通道："
                "OpsGen 的实时通道上挂着「在线执行脚本」，那等同于任意命令执行入口。"
                "需要时在 module.py 里把 allow_realtime 打开，并自行配上白名单与逐次确认。",
                status_code=403,
            )

        return await self._forward(request, runtime, safe)

    # ------------------------------------------------------------ 转发
    async def _forward(self, request: Request, runtime: ModuleRuntime, subpath: str) -> Response:
        spec = runtime.spec
        upstream_origin = f"http://127.0.0.1:{spec.internal_port}"

        # 保留原始查询串，但把中台的票据参数摘掉 —— 它是给中台看的，
        # 不是模块的业务参数，混进去会让模块的签名/缓存逻辑莫名其妙。
        pairs = [(k, v) for k, v in request.query_params.multi_items() if k != "_t"]
        query = urlencode(pairs)
        url = f"{upstream_origin}/{subpath}"
        if query:
            url += f"?{query}"

        try:
            body = await request.body()
        except Exception as exc:  # noqa: BLE001
            return self._deny(runtime, "读取请求体失败", str(exc), status_code=400)

        headers = self._build_headers(request, runtime)
        expected_cookie = _forwardable_cookies(request) if spec.allow_module_cookies else ""

        try:
            upstream = self._client.build_request(
                request.method, url, headers=headers, content=body
            )
        except httpx.HTTPError as exc:
            return self._deny(runtime, "构造转发请求失败", f"{type(exc).__name__}: {exc}", status_code=502)

        # 不变量：出站请求能带的 Cookie，**名字集合**必须恰好等于本次请求
        # 该带的那一份。HTTP 客户端的 Cookie 罐一旦把历史会话合并进来
        # （httpx 的默认行为，见 _disable_cookie_jar），这里就会多出名字而
        # 被拦下 —— 宁可这一次转发失败，也不能把别人的会话送进模块。
        expected_names = _cookie_names([expected_cookie]) if expected_cookie else set()
        emitted_names = _cookie_names(upstream.headers.get_list("cookie"))
        if emitted_names != expected_names:
            return self._deny(
                runtime,
                "Cookie 转发异常",
                "出站请求携带了非预期的 Cookie，已拦下这次转发："
                f"预期 {sorted(expected_names) or '不带 Cookie'}，"
                f"实际 {sorted(emitted_names)}。"
                "这通常意味着 HTTP 客户端开始自动保存模块会话了。",
                status_code=500,
            )

        try:
            response = await self._client.send(upstream, stream=True)
        except httpx.ConnectError as exc:
            return self._deny(
                runtime,
                "连不上模块进程",
                f"内部端口 {spec.internal_port} 拒绝连接（{exc}）。"
                f"进程可能刚崩溃 —— 看模块日志的最后几行。",
                status_code=502,
            )
        except httpx.HTTPError as exc:
            return self._deny(runtime, "转发失败", f"{type(exc).__name__}: {exc}", status_code=502)

        return await self._shape_response(request, runtime, response)

    def _build_headers(self, request: Request, runtime: ModuleRuntime) -> list[tuple[str, str]]:
        """构造给模块的请求头。

        四个要点：

        1. `Host` 转发**浏览器原本的 Host**，而不是模块的内部地址。
           Flask 用 Host 拼 `request.url_root`（OpsGen 的分享链接就是这么来的），
           转发原 Host 才能让它生成指向中台的地址 —— 再由内容改写补上前缀。
           如果换成 `127.0.0.1:8732`，用户复制到的分享链接就把内部端口暴露了。

        2. `cookie` 默认整条剔除：平台侧 Cookie 不该流向模块，模块保持「无会话」
           比让它偷偷带着一套平行会话要干净。但 `allow_module_cookies=True` 的
           模块（自带登录体系的完整应用，如 portal）例外 —— 不放行它连登录都
           登不上，因为登录接口刚发的 Cookie 下一个请求就被剥了。
           **例外只针对模块自己的 Cookie**：中台票据（`pw_mod_*`）无论如何都挡掉。

        3. `X-Forwarded-*` 全部补齐。上游把中台当反代用（uvicorn 的
           `--proxy-headers`、FastAPI 的 root_path、生成绝对 URL 时都要这几个头）。
           `Host` 一并转发，是为了让上游拼出的 URL 落在中台地址上而不是内部端口。

        4. `Accept-Encoding` 固定为 identity。上游一压缩，返回体就没法改写了。
           回环传输，这点带宽换可控性，划得来。

        5. `X-LocalDeck-Module-Ticket` —— 这条是给模块用的**共享凭据**，
           值就是本模块自己的票据（与中台令牌是两回事）。模块可选地用它来
           分辨「请求确实经中台代理而来」，从而在本地自用场景下省掉一层登录页。
           伪造不了：票据是每次运行新生成的随机串，且只经中台与模块之间传递。
           注意它**不是**中台令牌 —— 拿到它最多只能证明"这个请求来自中台"，
           够不到中台任何接口。
        """
        spec = runtime.spec
        mount = spec.mount
        client_host = request.headers.get("host", "")
        headers: list[tuple[str, str]] = []
        for name, value in request.headers.items():
            lowered = name.lower()
            if lowered in _REQUEST_DROP or lowered == "x-forwarded-for":
                continue
            headers.append((name, value))

        if spec.allow_module_cookies:
            cookies = _forwardable_cookies(request)
            if cookies:
                headers.append(("Cookie", cookies))

        if client_host:
            headers.append(("Host", client_host))
            headers.append(("X-Forwarded-Host", client_host))
        headers.append(("X-Forwarded-Prefix", mount))
        headers.append(("X-Forwarded-Proto", request.url.scheme))
        headers.append(("X-Real-IP", request.client.host if request.client else "127.0.0.1"))
        headers.append(("X-LocalDeck-Module-Ticket", runtime.ticket))
        headers.append(("Accept-Encoding", "identity"))
        return headers

    async def _shape_response(
        self, request: Request, runtime: ModuleRuntime, response: httpx.Response
    ) -> Response:
        spec = runtime.spec
        mount = spec.mount
        upstream_origin = f"http://127.0.0.1:{spec.internal_port}"

        content_type = response.headers.get("content-type", "")
        kind = is_rewritable(content_type)

        out_headers: dict[str, str] = {}
        for name, value in response.headers.items():
            if name.lower() in _RESPONSE_DROP:
                continue
            out_headers[name] = value

        location = response.headers.get("location")
        if location:
            out_headers["location"] = rewrite_location_header(location, mount, upstream_origin)

        # 模块自己的 Set-Cookie：把 Path 收进模块路径后再逐条重发。
        #
        # 但 `allow_module_cookies=False` 的模块要**两头一致**：既然它连请求
        # 侧的 Cookie 都收不到，再放它往浏览器里写会话，只会留下一个永远送
        # 不回去的死 Cookie —— 模块自己以为登录成功了，下一个请求却还是匿名，
        # 排查时会非常费解。所以这类模块的 Set-Cookie 直接丢掉。
        #
        # 注意：中台**自己的**模块票据 Cookie 不在这里下发，而是由安全中间件
        # 统一做（见 security.install_security）—— 因为 native / static 模块
        # 根本不走这个代理，放这里它们永远拿不到票据 Cookie。
        cookies = (
            [rewrite_set_cookie(v, mount) for v in response.headers.get_list("set-cookie")]
            if spec.allow_module_cookies
            else []
        )

        # 中台自己的模块票据 Cookie 曾经在这里下发，现已上移到安全中间件 ——
        # 那里才能同时覆盖 native / static 这类不走代理的模块。
        # 见 security.install_security 里 `pending_ticket_cookie` 一段。

        length = response.headers.get("content-length")
        too_big = False
        if length:
            try:
                too_big = int(length) > _MAX_REWRITE_BYTES
            except ValueError:
                too_big = False

        # 非文本 / 超大 → 原样流式转发（下载 ZIP 走这条）
        if not kind or too_big:
            stream = StreamingResponse(
                self._stream(response), status_code=response.status_code, headers=out_headers
            )
            for cookie in cookies:
                stream.headers.append("set-cookie", cookie)
            return stream

        raw = await response.aread()
        await response.aclose()

        # 上游没报 Content-Length 时，读完才知道有多大。真超了就原样给出去。
        if len(raw) > _MAX_REWRITE_BYTES:
            plain = Response(
                content=raw, status_code=response.status_code, headers=out_headers
            )
            for cookie in cookies:
                plain.headers.append("set-cookie", cookie)
            return plain

        text = raw.decode(response.charset_encoding or "utf-8", errors="replace")

        origins: list[str] = []
        client_host = request.headers.get("host")
        if client_host:
            origins = [f"http://{client_host}", f"https://{client_host}"]

        if kind == "html":
            if not spec.allow_external_assets:
                text, removed = strip_external_subresources(text)
                if removed:
                    runtime.announce_once(
                        "external-assets-stripped",
                        "[中台] 已剥离页面里的外部 CDN 资源（零网络上传、无遥测）："
                        + "；".join(removed[:5])
                        + " —— 需要保留就在 module.py 里把 allow_external_assets 打开。"
                        "（本条只提示一次，避免每个页面都刷一遍）",
                    )
            text = rewrite_html(text, mount, origins=origins)
            text = inject_shim(
                text,
                mount=mount,
                realtime=spec.allow_realtime,
                external_assets=spec.allow_external_assets,
            )
        else:
            text = rewrite_css(text, mount)

        # 我们把编码统一成 utf-8 了，所以 Content-Type 也要照着说，
        # 否则上游若写的是 gbk，浏览器会按 gbk 去解 utf-8 的字节。
        base_type = (content_type.split(";", 1)[0] or "text/plain").strip()
        out_headers.pop("Content-Type", None)
        out_headers.pop("content-type", None)

        result = Response(
            content=text.encode("utf-8"),
            status_code=response.status_code,
            headers=out_headers,
            media_type=f"{base_type}; charset=utf-8",
        )
        for cookie in cookies:
            result.headers.append("set-cookie", cookie)
        return result

    @staticmethod
    async def _stream(response: httpx.Response):
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    # ------------------------------------------------------------ 杂
    @staticmethod
    def _is_realtime_path(subpath: str) -> bool:
        head = subpath.split("/", 1)[0].lower()
        return head in {"socket.io", "ws", "websocket"}

    def _deny(
        self, runtime: ModuleRuntime, title: str, detail: str, *, status_code: int
    ) -> JSONResponse:
        payload = {
            "detail": f"{title}：{detail}",
            "module": runtime.spec.id,
            "blocked_by": "localdeck",
        }
        # 同一条拒绝消息在日志里只留最近一次，避免 socket.io 重连把日志刷爆
        stamp = f"[中台] 拒绝「{title}」（{time.strftime('%H:%M:%S')}）"
        if not runtime.log or not runtime.log[-1].startswith(f"[中台] 拒绝「{title}」"):
            runtime.log.append(stamp)
        return JSONResponse(payload, status_code=status_code)
