"""反向代理端到端验证：起一个真实的内网模拟服务，经门户网关访问并断言结果。

运行：python scripts/e2e_proxy.py
"""

from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

TMP_DB = Path(tempfile.gettempdir()) / "devtoolbox_e2e.db"
for suffix in ("", "-wal", "-shm"):
    candidate = Path(str(TMP_DB) + suffix)
    if candidate.exists():
        candidate.unlink()

TARGET_PORT = 9099
TARGET_BASE = f"http://127.0.0.1:{TARGET_PORT}"

os.environ.setdefault("SECRET_KEY", "e2e-test-secret-key-please-ignore")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"
os.environ["BOOTSTRAP_ADMIN_USERNAME"] = "e2eadmin"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "E2eAdmin#2026"
os.environ["LOG_JSON"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"
os.environ["PROXY_VERIFY_TLS"] = "false"

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from mock_target_app import app as target_app  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")


def wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.15)
    return False


def is_port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def start_target() -> uvicorn.Server:
    config = uvicorn.Config(target_app, host="127.0.0.1", port=TARGET_PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    return server


def main() -> int:
    print(f"\n启动模拟内网服务 {TARGET_BASE} ...")
    # 端口被占用时必须直接失败：否则 uvicorn 绑不上端口、测试会静默打到
    # 别人已经跑着的旧版本 mock 上，得到一堆看不懂的 404。
    if is_port_open("127.0.0.1", TARGET_PORT):
        print(
            f"  \033[31m端口 {TARGET_PORT} 已被占用\033[0m —— 可能有一个旧版模拟服务还在跑。\n"
            f"  请先停掉它再重跑，否则测试会打到旧代码上：\n"
            f"    Windows:  netstat -ano | findstr :{TARGET_PORT}   然后 taskkill /F /PID <pid>\n"
            f"    Linux:    lsof -i:{TARGET_PORT}"
        )
        return 1
    start_target()
    if not wait_for_port("127.0.0.1", TARGET_PORT):
        print("  \033[31m无法启动模拟目标服务\033[0m")
        return 1

    # 直连目标一次，确认模拟服务本身正常
    probe = httpx.get(f"{TARGET_BASE}/", timeout=5)
    check("模拟内网服务自身可访问", probe.status_code == 200, f"status={probe.status_code}")

    with TestClient(app, base_url="http://portal.test") as client:
        r = client.post("/api/auth/login", json={"username": "e2eadmin", "password": "E2eAdmin#2026"})
        check("登录门户成功", r.status_code == 200, r.text[:160])

        r = client.post(
            "/api/cards",
            json={
                "slug": "mocktarget",
                "title": "模拟内网系统",
                "description": "e2e",
                "target_url": f"{TARGET_BASE}/",
                "open_mode": "proxy",
            },
        )
        check("创建代理卡片成功", r.status_code == 201, r.text[:200])
        check("launch_url 指向网关", r.json().get("launch_url") == "/gw/mocktarget/", str(r.json().get("launch_url")))

        print("\n[1] 入口与 HTML 重写")
        r = client.get("/gw/mocktarget", follow_redirects=False)
        check("无尾斜杠入口 308", r.status_code == 308, f"status={r.status_code}")

        r = client.get("/gw/mocktarget/")
        check("首页经网关返回 200", r.status_code == 200, f"status={r.status_code}")
        html = r.text
        check("注入 <base> 标签", '<base href="/gw/mocktarget/">' in html, html[:400])
        check("重写 CSS 根路径", 'href="/gw/mocktarget/static/app.css"' in html, html[:600])
        check("重写 JS 根路径", 'src="/gw/mocktarget/static/app.js"' in html, html[:600])
        check("重写图片根路径", 'src="/gw/mocktarget/img/logo.png"' in html, html[:600])
        check("重写内联 style url()", "url(/gw/mocktarget/img/bg.png)" in html, html[:800])
        check("重写表单 action", 'action="/gw/mocktarget/submit"' in html, html[:800])
        check("协议相对地址 www 未被误改", 'href="//cdn.example.com/lib.js"' in html, html[:800])
        check("站外绝对地址未被改写", 'href="https://example.com/external"' in html, html[:800])
        # —— 与目标同源的绝对地址：真实场景里 Jenkins 的主题 CSS 就是这类 ——
        check(
            "重写同源绝对地址的 CSS 引用",
            'href="/gw/mocktarget/static/theme-dark/theme.css"' in html,
            html[:900],
        )
        check(
            "重写同源绝对地址的图片引用",
            'src="/gw/mocktarget/img/abs-logo.png"' in html,
            html[:1200],
        )
        check(
            "重写同源协议相对地址的图片引用",
            'src="/gw/mocktarget/img/abs-logo.png"' in html,
            html[:1200],
        )
        check(
            "三处 /page2 链接（根路径/绝对/协议相对）都被收进网关",
            html.count('href="/gw/mocktarget/page2"') == 3,
            str(html.count('href="/gw/mocktarget/page2"')),
        )
        # 注意：注入的 shim 里**必然**含有目标 origin（它要靠这个字符串认地址），
        # 所以这里只能断言"URL 属性里不再有目标 host"，不能断言整页不含该字符串。
        leftover = re.findall(
            r'(?:src|href|action|poster|data-src|formaction)\s*=\s*["\']('
            + re.escape(TARGET_BASE)
            + r'[^"\']*)["\']',
            html,
        )
        check("URL 属性里不再残留指向目标 host 的绝对地址", not leftover, str(leftover[:3]))
        # 运行时 shim：补 <base> 覆盖不到的「根路径绝对地址」
        check("注入客户端 shim", 'data-dtb-gateway="/gw/mocktarget/"' in html, html[:500])
        check("shim 排在 <base> 之前", html.find("data-dtb-gateway") < html.find("<base "), "顺序不对")
        check("shim 声明了网关前缀", '"/gw/mocktarget/"' in html and "__dtbGatewayPrefix" in html, html[:500])

        print("\n[2] 静态资源与内容类型透传")
        r = client.get("/gw/mocktarget/static/app.css")
        check("CSS 经网关可加载", r.status_code == 200 and "text/css" in r.headers.get("content-type", ""), r.text[:160])
        check("CSS 内 url() 被重写", "url(/gw/mocktarget/img/logo.png)" in r.text, r.text[:200])
        check(
            "CSS 内同源绝对 url() 也被重写",
            "url(/gw/mocktarget/img/bg.png)" in r.text,
            r.text[:320],
        )

        # 只被绝对地址引用的 CSS（模拟 Jenkins 的 /theme-dark/theme.css）
        r = client.get("/gw/mocktarget/static/theme-dark/theme.css")
        check(
            "绝对地址引用的 CSS 经网关可加载",
            r.status_code == 200 and "text/css" in r.headers.get("content-type", ""),
            f"status={r.status_code} ct={r.headers.get('content-type')}",
        )
        check(
            "CSS 内同源绝对 url() 被改写",
            "url(/gw/mocktarget/img/bg.png)" in r.text,
            r.text[:320],
        )
        check(
            "CSS 内外链 url() 保持原样",
            "url(https://example.com/bg.png)" in r.text,
            r.text[:320],
        )

        r = client.get("/gw/mocktarget/img/logo.png")
        check("图片二进制透传", r.status_code == 200 and r.content.startswith(b"\x89PNG"), r.content[:20])

        r = client.get("/gw/mocktarget/api/data")
        check("JSON 接口不被重写", r.status_code == 200 and r.json()["items"] == [1, 2, 3], r.text[:200])

        print("\n[3] POST / 表单")
        r = client.post("/gw/mocktarget/submit", data={"name": "来自门户"})
        check("表单 POST 转发成功", r.status_code == 200, r.text[:200])
        check("POST 参数正确送达", r.json().get("received_name") == "来自门户", r.text[:200])

        print("\n[4] 编码与压缩")
        r = client.get("/gw/mocktarget/gbk")
        check("GBK 页面返回 200", r.status_code == 200, f"status={r.status_code}")
        check("GBK 页面被注入 base（字节级重写）", b'<base href="/gw/mocktarget/">' in r.content, str(r.content[:260]))
        try:
            decoded = r.content.decode("gbk")
            check("GBK 中文未被破坏", "设备迁移与字典录入" in decoded, decoded[:200])
        except UnicodeDecodeError as exc:
            check("GBK 中文未被破坏", False, str(exc))

        r = client.get("/gw/mocktarget/gzip")
        check("gzip HTML 经网关返回 200", r.status_code == 200, f"status={r.status_code}")
        check("gzip HTML 已重写", '<base href="/gw/mocktarget/">' in r.text, r.text[:260])
        check("已移除 content-encoding 避免二次解压", r.headers.get("content-encoding") in (None, "identity"), str(r.headers.get("content-encoding")))

        print("\n[5] Cookie 修正")
        r = client.get("/gw/mocktarget/setcookie")
        cookies = r.headers.get_list("set-cookie")
        joined = " | ".join(cookies)
        check("返回 Set-Cookie", len(cookies) >= 1, joined or "(无)")
        check("剥离 Domain 属性", "domain=" not in joined.lower(), joined)
        check("Path 重写到网关前缀", "Path=/gw/mocktarget" in joined, joined)
        check("http 场景下降级 SameSite=None", "samesite=none" not in joined.lower(), joined)
        check("http 场景下移除 Secure", "secure" not in joined.lower(), joined)

        print("\n[6] 重定向改写")
        r = client.get("/gw/mocktarget/redirect", follow_redirects=False)
        check("站内重定向 302", r.status_code == 302, f"status={r.status_code}")
        check("Location 改写为网关路径", r.headers.get("location") == "/gw/mocktarget/page2", str(r.headers.get("location")))

        r = client.get("/gw/mocktarget/redirect", follow_redirects=True)
        check("跟随重定向后拿到第二页", r.status_code == 200 and "第二页" in r.text, r.text[:200])

        r = client.get("/gw/mocktarget/redirect-external", follow_redirects=False)
        check("站外重定向保持原样", r.headers.get("location") == "https://example.com/external", str(r.headers.get("location")))

        print("\n[7] 大响应流式透传")
        r = client.get("/gw/mocktarget/big")
        check("2MB 响应完整透传", r.status_code == 200 and len(r.content) == 200 * 10240, f"len={len(r.content)}")

        print("\n[8] 安全头处理")
        r = client.get("/gw/mocktarget/frame-blocked")
        check("X-Frame-Options 已剥离", r.headers.get("x-frame-options") is None, str(r.headers.get("x-frame-options")))
        check("CSP 已剥离", r.headers.get("content-security-policy") is None, str(r.headers.get("content-security-policy")))

        print("\n[9] 门户自身 Cookie 不外泄")
        from app.services.proxy_service import _filter_cookie_header

        filtered = _filter_cookie_header("dtb_access=SECRET_TOKEN; dtb_refresh=R; SID=ok", "mocktarget")
        check("门户令牌被过滤", "dtb_access" not in filtered and "SECRET" not in filtered, filtered)
        check("第三方 Cookie 保留", "SID=ok" in filtered, filtered)

        print("\n[10] WebSocket 透传")
        from starlette.websockets import WebSocketDisconnect

        # 浏览器在 WS 握手时会自动带上同源 Cookie；TestClient 不会，
        # 因此这里显式传递，模拟真实浏览器行为。
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
        try:
            with client.websocket_connect(
                "/gw/mocktarget/ws", headers={"cookie": cookie_header}
            ) as ws:
                ws.send_text("hello-ws")
                reply = ws.receive_text()
                check("WebSocket 回显成功", reply == "echo:hello-ws", f"got={reply!r}")
                ws.send_text("第二条")
                check("WebSocket 可多轮收发", ws.receive_text() == "echo:第二条")
        except WebSocketDisconnect as exc:
            check("WebSocket 回显成功", False, f"WebSocketDisconnect code={exc.code} reason={exc.reason!r}")
        except Exception as exc:  # noqa: BLE001
            check("WebSocket 回显成功", False, f"{type(exc).__name__}: {exc}")

        print("\n[10b] WebSocket 鉴权")
        try:
            with client.websocket_connect("/gw/mocktarget/ws") as ws:
                ws.receive_text()
            check("无 Cookie 的 WS 被拒绝", False, "竟然连上了")
        except WebSocketDisconnect as exc:
            check("无 Cookie 的 WS 被拒绝(1008)", exc.code == 1008, f"code={exc.code}")
        except Exception as exc:  # noqa: BLE001
            check("无 Cookie 的 WS 被拒绝(1008)", False, f"{type(exc).__name__}: {exc}")

        print("\n[11] 网关鉴权")
        with TestClient(app, base_url="http://portal.test") as anon:
            r = anon.get("/gw/mocktarget/")
            check("未登录访问网关被拒 401", r.status_code == 401, f"status={r.status_code}")

        print("\n[12] 根路径运行时请求（内网页面里 JS 拼出来的 URL）")
        # 真实场景：被代理页面的 JS 执行 fetch('/api/v1/game-config')。
        # <base> 只影响相对路径，这类根路径请求会打到门户域名根上，
        # 因此需要 shim（浏览器内）+ 服务端 Referer 兜底（这里验证的是后者，
        # 因为 TestClient 不会执行 JS）。
        gw_referer = {"referer": "http://portal.test/gw/mocktarget/"}

        r = client.get("/api/v1/game-config", headers=gw_referer)
        check("带网关 Referer 的根路径接口被兜底转发", r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
        check("拿到的是内网系统的数据", r.json().get("from") == "mock-target", r.text[:200])

        r = client.post("/api/v1/game-config", headers=gw_referer, json={"stage": 3})
        check("POST 根路径接口同样被转发", r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
        check("POST 请求体完整送达", r.json().get("echo") == {"stage": 3}, r.text[:200])

        r = client.get("/api/v1/game-config?level=2", headers=gw_referer)
        check("查询串被保留", r.status_code == 200 and r.json().get("from") == "mock-target", r.text[:200])

        # —— 反向保证：门户自身不能被打扰 ——
        r = client.get("/api/v1/game-config")
        check("无 Referer 时门户照常返回 404", r.status_code == 404, f"status={r.status_code}")
        check("404 是门户的规范错误体", r.json().get("error", {}).get("code") == "not_found", r.text[:160])

        r = client.get("/api/v1/game-config", headers={"referer": "http://portal.test/"})
        check("Referer 指向门户首页时不触发转发", r.status_code == 404, f"status={r.status_code}")

        r = client.get("/api/cards", headers=gw_referer)
        check("门户自身接口不会被转发", r.status_code == 200 and isinstance(r.json(), list), f"status={r.status_code} {r.text[:160]}")

        r = client.get("/api/cards/whatever/extra", headers=gw_referer)
        check("门户 api/cards 命名空间下的未匹配路径仍走 404", r.status_code == 404, f"status={r.status_code}")

        r = client.get("/uploads/icons/nope.png", headers=gw_referer)
        check("门户 uploads 命名空间不受影响", r.status_code == 404, f"status={r.status_code}")

        r = client.get("/api/v1/game-config", headers={"referer": "http://portal.test/gw/nosuchcard/"})
        check("Referer 指向不存在的卡片时退回 404", r.status_code == 404, f"status={r.status_code}")

        r = client.post(
            "/api/cards",
            json={
                "slug": "directcard",
                "title": "直连卡片",
                "target_url": f"{TARGET_BASE}/",
                "open_mode": "direct",
            },
        )
        check("创建直连卡片成功", r.status_code == 201, r.text[:200])
        r = client.get("/api/v1/game-config", headers={"referer": "http://portal.test/gw/directcard/"})
        check("直连模式的卡片不会被兜底转发", r.status_code == 404, f"status={r.status_code}")

        print("\n[13] 兜底转发的安全边界")
        with TestClient(app, base_url="http://portal.test") as anon:
            r = anon.get("/api/v1/game-config", headers=gw_referer)
            check("未登录伪造 Referer 被拒 401", r.status_code == 401, f"status={r.status_code}")
            check("401 是门户的规范错误体", r.json().get("error", {}).get("code") == "unauthorized", r.text[:160])

        from app.services.proxy_service import client_shim, gateway_slug_from_referer, is_portal_owned_path

        try:
            client_shim("/gw/mocktarget/").decode("ascii")
            check("shim 是纯 ASCII（GBK 页面注入后不会乱码）", True)
        except UnicodeDecodeError as exc:
            check("shim 是纯 ASCII（GBK 页面注入后不会乱码）", False, str(exc))

        shim_with_target = client_shim("/gw/mocktarget/", target_url=f"{TARGET_BASE}/")
        try:
            shim_with_target.decode("ascii")
            check("带目标地址的 shim 仍是纯 ASCII", True)
        except UnicodeDecodeError as exc:
            check("带目标地址的 shim 仍是纯 ASCII", False, str(exc))
        check(
            "shim 注入了目标 origin",
            TARGET_BASE.encode() in shim_with_target,
            shim_with_target[:200].decode("latin-1"),
        )
        check(
            "shim 无残留占位符",
            b"__ORIGIN__" not in shim_with_target and b"__BASEPATH__" not in shim_with_target,
            "占位符未被替换",
        )

        from app.services.proxy_service import _rewrite_absolute_url, _url_origin_and_base

        origin, base_path = _url_origin_and_base(f"{TARGET_BASE}/")
        check("目标 origin 解析正确", origin == TARGET_BASE, origin)

        def _abs(u: str) -> str | None:
            return _rewrite_absolute_url(
                u, origin=origin, base_path=base_path, prefix_bare="/gw/x"
            )

        check("绝对地址：同源命中", _abs(f"{TARGET_BASE}/a.css") == "/gw/x/a.css", str(_abs(f"{TARGET_BASE}/a.css")))
        check("绝对地址：同源根路径命中", _abs(f"{TARGET_BASE}/") == "/gw/x/", str(_abs(f"{TARGET_BASE}/")))
        check("绝对地址：查询串保留", _abs(f"{TARGET_BASE}/a?q=1") == "/gw/x/a?q=1", str(_abs(f"{TARGET_BASE}/a?q=1")))
        check("绝对地址：协议相对命中", _abs("//127.0.0.1:9099/a.png") == "/gw/x/a.png", str(_abs("//127.0.0.1:9099/a.png")))
        check("绝对地址：跨主机放行", _abs("http://example.com/a.css") is None, str(_abs("http://example.com/a.css")))
        check("绝对地址：端口不同放行", _abs("http://127.0.0.1:1/a.css") is None, str(_abs("http://127.0.0.1:1/a.css")))
        check("绝对地址：协议不同放行", _abs("https://127.0.0.1:9099/a.css") is None, str(_abs("https://127.0.0.1:9099/a.css")))
        check("绝对地址：协议相对跨主机放行", _abs("//cdn.example.com/a.js") is None, str(_abs("//cdn.example.com/a.js")))

        check("Referer 解析：带 slug", gateway_slug_from_referer("http://h/gw/mocktarget/a/b") == "mocktarget")
        check("Referer 解析：无 gw 前缀返回 None", gateway_slug_from_referer("http://h/cards") is None)
        check("Referer 解析：空值返回 None", gateway_slug_from_referer("") is None)
        check("Referer 解析：只有 /gw/ 返回 None", gateway_slug_from_referer("http://h/gw/") is None)
        check("门户命名空间：/api/cards 命中", is_portal_owned_path("api/cards"))
        check("门户命名空间：/api/v1/xx 不命中", not is_portal_owned_path("api/v1/game-config"))
        check("门户命名空间：/uploads 命中", is_portal_owned_path("uploads/icons/a.png"))
        check("门户命名空间：根路径命中", is_portal_owned_path(""))

        from app.services.proxy_service import rewrite_html

        once = rewrite_html(html.encode("utf-8"), prefix="/gw/mocktarget/")
        check("重复重写不会注入第二个 shim", once.count(b"data-dtb-gateway") == 1, str(once.count(b"data-dtb-gateway")))
        check("重复重写不会注入第二个 <base>", once.count(b"<base ") == 1, str(once.count(b"<base ")))

        print("\n[14] 目标指向具体页面（不以 / 结尾）时的作用域")
        from app.services.proxy_service import _join_target

        # 真实案例：Jenkins 卡片填 http://host/login（它的站点根 / 返回 403）。
        # 这种目标属于"页面作用域"：入口原样打开该页面，页面里的 /static/** 按
        # 站点绝对路径解析；若沿用"目录作用域"会被拼成 /login/static/** 而全部 404。
        page_target = f"{TARGET_BASE}/index.html"
        check(
            "页面作用域：入口原样",
            _join_target(page_target, "", "") == page_target,
            _join_target(page_target, "", ""),
        )
        check(
            "页面作用域：子路径按站点绝对路径",
            _join_target(page_target, "static/app.css", "") == f"{TARGET_BASE}/static/app.css",
            _join_target(page_target, "static/app.css", ""),
        )
        check(
            "目录作用域：子路径挂在目录下（行为不变）",
            _join_target(f"{TARGET_BASE}/sub/", "x.css", "") == f"{TARGET_BASE}/sub/x.css",
            _join_target(f"{TARGET_BASE}/sub/", "x.css", ""),
        )
        check(
            "目录作用域：入口原样（行为不变）",
            _join_target(f"{TARGET_BASE}/sub/", "", "") == f"{TARGET_BASE}/sub/",
            _join_target(f"{TARGET_BASE}/sub/", "", ""),
        )
        check(
            "站点根：子路径（行为不变）",
            _join_target(f"{TARGET_BASE}/", "static/app.css", "")
            == f"{TARGET_BASE}/static/app.css",
            _join_target(f"{TARGET_BASE}/", "static/app.css", ""),
        )

        r = client.post(
            "/api/cards",
            json={
                "slug": "pagescope",
                "title": "页面作用域卡片",
                "description": "e2e",
                "target_url": page_target,
                "open_mode": "proxy",
            },
        )
        check("创建页面作用域卡片成功", r.status_code == 201, r.text[:200])

        r = client.get("/gw/pagescope/")
        check(
            "入口原样命中该页面",
            r.status_code == 200 and "内网示例系统" in r.text,
            f"status={r.status_code} {r.text[:160]}",
        )
        check(
            "页面作用域下同源绝对地址被收进网关",
            'href="/gw/pagescope/static/theme-dark/theme.css"' in r.text,
            r.text[:500],
        )
        r = client.get("/gw/pagescope/static/app.css")
        check(
            "静态资源按站点绝对路径解析（不再是 /index.html/static/…）",
            r.status_code == 200 and "text/css" in r.headers.get("content-type", ""),
            f"status={r.status_code} ct={r.headers.get('content-type')}",
        )
        r = client.get("/gw/pagescope/static/theme-dark/theme.css")
        check(
            "绝对地址引用的主题 CSS 可经网关加载",
            r.status_code == 200 and "text/css" in r.headers.get("content-type", ""),
            f"status={r.status_code}",
        )
        r = client.get("/gw/pagescope/img/abs-logo.png")
        check(
            "同源绝对地址图片可经网关加载",
            r.status_code == 200 and r.content.startswith(b"\x89PNG"),
            f"status={r.status_code} {r.content[:12]}",
        )

    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for item in FAILED:
            print(f"  - {item}")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
