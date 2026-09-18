"""部署自检：对**真实运行中**的服务发起 HTTP 请求，验证整条链路。

与 smoke.py / e2e_proxy.py 的区别：
- 那两个用 TestClient 在进程内跑，用于开发期回归；
- 本脚本打真实端口，用于跳板机部署后确认「服务真的能用了」。

用法：
    python scripts/verify_deployment.py                       # 默认 http://127.0.0.1:8000
    DTB_BASE=http://10.0.0.5:8000 python scripts/verify_deployment.py

凭据来源：优先读环境变量 DTB_USERNAME / DTB_PASSWORD，其次读项目根目录 .env。
脚本不会打印密码，只打印校验结果。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

BASE = os.environ.get("DTB_BASE", "http://127.0.0.1:8000").rstrip("/")
MOCK_TARGET = os.environ.get("DTB_MOCK_TARGET", "")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else f"{name} :: {detail}")
    color = "\033[32m" if ok else "\033[31m"
    print(f"  {color}{'PASS' if ok else 'FAIL'}\033[0m  {name}" + ("" if ok else f"  {detail}"))


def read_env_file() -> dict[str, str]:
    """极简 .env 解析，仅用于取自检所需的用户名/密码。"""
    values: dict[str, str] = {}
    env_path = PROJECT_DIR / ".env"
    if not env_path.is_file():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def make_opener(
    jar: http.cookiejar.CookieJar, *, follow_redirects: bool
) -> urllib.request.OpenerDirector:
    """构造 opener。

    - 显式禁用代理：本机若设了 HTTP_PROXY，会把对内网/本机的请求也一起劫持走
    - follow_redirects=False 时保留 3xx 原样返回，便于断言 308/302 的 Location
    """
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
    ]
    if not follow_redirects:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """让 3xx 不被自动跟随，直接把状态码与 Location 交给调用方。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class Client:
    def __init__(
        self, *, follow_redirects: bool = True, jar: http.cookiejar.CookieJar | None = None
    ) -> None:
        self.jar = jar if jar is not None else http.cookiejar.CookieJar()
        self.opener = make_opener(self.jar, follow_redirects=follow_redirects)

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: float = 20.0,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, object]:
        data = json.dumps(body).encode() if body is not None else None
        merged = {"Accept": "application/json"}
        if data is not None:
            merged["Content-Type"] = "application/json"
        if headers:
            merged.update(headers)
        request = urllib.request.Request(f"{BASE}{path}", data=data, headers=merged, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                # 直接返回 HTTPMessage（大小写不敏感，且能取到重复的 Set-Cookie）
                return response.status, response.read().decode("utf-8", "replace"), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), exc.headers


def main() -> int:
    env = read_env_file()
    username = os.environ.get("DTB_USERNAME") or env.get("BOOTSTRAP_ADMIN_USERNAME", "admin")
    password = os.environ.get("DTB_PASSWORD") or env.get("BOOTSTRAP_ADMIN_PASSWORD", "")

    print(f"\n目标服务：{BASE}")

    reachable = True
    try:
        with socket.create_connection((urllib.parse.urlsplit(BASE).hostname, urllib.parse.urlsplit(BASE).port or 80), timeout=4):
            pass
    except OSError as exc:
        reachable = False
        print(f"  \033[31m无法连接 {BASE}：{exc}\033[0m")

    print("\n[1] 存活与就绪")
    client = Client()
    if reachable:
        status, text, _ = client.call("GET", "/health")
        check("GET /health 返回 200", status == 200, f"status={status} {text[:120]}")
        try:
            payload = json.loads(text)
            check("健康检查含应用名与版本", "app" in payload and "version" in payload, text[:160])
        except json.JSONDecodeError:
            check("健康检查返回 JSON", False, text[:160])

        status, text, _ = client.call("GET", "/ready")
        try:
            ready = json.loads(text)
            db_ok = ready.get("checks", {}).get("database") == "ok"
        except json.JSONDecodeError:
            db_ok = False
        check("GET /ready 依赖就绪", status == 200 and db_ok, f"status={status} {text[:160]}")
    else:
        check("GET /health 返回 200", False, "服务不可达")
        check("GET /ready 依赖就绪", False, "服务不可达")

    print("\n[2] 前端静态资源")
    if reachable:
        status, text, _ = client.call("GET", "/")
        check("首页返回 HTML", status == 200 and "<div id=\"root\">" in text, f"status={status}")
        check("首页引用打包资源", "/assets/" in text, text[:200])
        check("首页无 sourcemap 泄漏", ".js.map" not in text, text[:200])

        status, text, _ = client.call("GET", "/favicon.svg")
        check("站点图标可访问", status == 200, f"status={status}")

    print("\n[3] 认证与权限")
    if reachable:
        status, text, _ = client.call("GET", "/api/cards")
        check("未登录访问卡片被拒 401", status == 401, f"status={status}")
        check("错误响应为规范结构", '"error"' in text and '"code"' in text, text[:160])

        if not password:
            print("  \033[33mSKIP\033[0m  未提供管理员密码（.env 里 BOOTSTRAP_ADMIN_PASSWORD 为空，可能是随机生成）")
            print("        可改用：DTB_USERNAME=xxx DTB_PASSWORD=yyy python scripts/verify_deployment.py")
        else:
            status, text, headers = client.call(
                "POST", "/api/auth/login", {"username": username, "password": password}
            )
            check("管理员登录成功", status == 200, f"status={status} {text[:160]}")
            check("认证走 Cookie（响应体不含令牌）", "dtb_access" not in text, "响应体出现令牌名")
            raw_cookies = headers.get_all("set-cookie") or []  # type: ignore[union-attr]
            cookie_blob = " | ".join(raw_cookies)
            check("下发访问令牌 Cookie", "dtb_access=" in cookie_blob, cookie_blob[:200] or "(无 Set-Cookie)")
            check("Cookie 标记 HttpOnly", "httponly" in cookie_blob.lower(), cookie_blob[:200] or "(无 Set-Cookie)")

    print("\n[4] 卡片数据")
    cards: list[dict] = []
    if reachable and password:
        status, text, _ = client.call("GET", "/api/cards")
        check("卡片列表返回 200", status == 200, f"status={status} {text[:160]}")
        try:
            cards = json.loads(text)
        except json.JSONDecodeError:
            cards = []
        check("内置示例卡片已就绪", len(cards) >= 9, f"实际 {len(cards)} 张")
        if cards:
            sample = cards[0]
            check("卡片带 launch_url", bool(sample.get("launch_url")), str(sample)[:160])
            check(
                "代理模式卡片指向网关",
                all(
                    card["launch_url"].startswith("/gw/") or card["open_mode"] == "direct"
                    for card in cards
                ),
                str([(c["slug"], c["open_mode"], c["launch_url"]) for c in cards[:3]]),
            )
        # 未配置真实地址时给出明确提醒
        placeholders = [c for c in cards if "192.168.1." in c.get("target_url", "")]
        if placeholders:
            print(
                f"  \033[33m提示\033[0m  有 {len(placeholders)} 张卡片仍是示例地址（192.168.1.x），"
                "请到「功能管理」改成真实内网地址"
            )

    print("\n[5] 反向代理链路")
    # 自检会临时把某张卡片的 target_url / open_mode 改到本地 mock 服务上，
    # 跑完必须还原 —— 否则用户会莫名其妙发现导航页少了一个真实地址。
    #
    # 这里必须连 open_mode 一起改成 proxy：网关与 Referer 兜底转发只对代理模式的
    # 卡片生效（直连卡片的流量根本不经过跳板机）。只改 target_url 的话，
    # 卡片恰好是直连模式时用例会莫名失败，还看不出是数据状态的问题。
    restore_target: tuple[int, str, str] | None = None
    if reachable and password and cards:
        target_card = next((c for c in cards if c["slug"] == "address-nav"), cards[0])
        if MOCK_TARGET:
            original_target = target_card.get("target_url", "")
            original_mode = target_card.get("open_mode", "proxy")
            status, text, _ = client.call(
                "PATCH",
                f"/api/cards/{target_card['id']}",
                {"target_url": MOCK_TARGET, "open_mode": "proxy"},
            )
            check("把卡片指向自检目标成功", status == 200, f"status={status} {text[:160]}")
            if status == 200 and original_target:
                restore_target = (target_card["id"], original_target, original_mode)
        else:
            print("  \033[33mSKIP\033[0m  未设置 DTB_MOCK_TARGET，跳过实际转发验证")
            print("        完整验证：另开一个终端，在 backend/ 目录下依次执行这两条")
            # 解释器写 sys.executable 而不是硬编码 .venv 路径：
            # venv 在项目根，不在 backend/ 下，硬编码会指向不存在的文件。
            print(f'          "{sys.executable}" -m uvicorn mock_target_app:app --app-dir scripts --port 9099')
            print(f'          DTB_MOCK_TARGET=http://127.0.0.1:9099 "{sys.executable}" scripts/verify_deployment.py')
            print("        （PowerShell 下设环境变量：$env:DTB_MOCK_TARGET='http://127.0.0.1:9099'）")

        slug = target_card["slug"]
        strict = Client(follow_redirects=False, jar=client.jar)
        status, text, headers = strict.call("GET", f"/gw/{slug}", timeout=10)
        check("网关入口 308 补尾斜杠", status == 308, f"status={status}")
        check(
            "308 指向带斜杠路径",
            headers.get("Location") == f"/gw/{slug}/",  # type: ignore[union-attr]
            str(headers.get("Location")),  # type: ignore[union-attr]
        )

        status, text, _ = client.call("GET", f"/gw/{slug}/", timeout=15)
        if MOCK_TARGET:
            check("经网关获取内网页面 200", status == 200, f"status={status} {text[:200]}")
            check("注入 <base> 便于相对路径解析", f'<base href="/gw/{slug}/">' in text, text[:400])
            check("注入客户端 shim", f'data-dtb-gateway="/gw/{slug}/"' in text, text[:500])
            check("shim 排在 <base> 之前", text.find("data-dtb-gateway") < text.find("<base "), "顺序不对")
            check("根路径资源被重写到网关下", f'"/gw/{slug}/static/' in text, text[:600])

            asset = re.search(rf'/gw/{slug}(/static/[^"\']+)', text)
            if asset:
                status, _, _ = client.call("GET", f"/gw/{slug}{asset.group(1)}", timeout=10)
                check("内网静态资源可经网关加载", status == 200, f"asset={asset.group(1)} status={status}")
            else:
                check("内网静态资源可经网关加载", False, "页面中未找到可验证的静态资源")

            # 与目标同源的**绝对地址**（`http://<目标host>/x`）也必须被收进网关：
            # Jenkins 的主题 CSS 就是这种形式，客户端在跳板机场景下访问不到那个 host，
            # 一旦原样放行就会 status=0、样式整个丢失。
            check(
                "同源绝对地址被重写到网关下",
                f'"/gw/{slug}/static/theme-dark/theme.css"' in text,
                text[:600],
            )
            if re.search(r"http://[^\s\"']+/static/theme-dark/theme\.css", text):
                check("页面里不再残留指向目标 host 的绝对 URL 属性", False, "仍有绝对地址未被改写")
            else:
                check("页面里不再残留指向目标 host 的绝对 URL 属性", True)

            status, _, _ = client.call(
                "GET", f"/gw/{slug}/static/theme-dark/theme.css", timeout=10
            )
            check("绝对地址引用的资源可经网关加载", status == 200, f"status={status}")

            # 内网页面里 JS 拼出来的根路径请求（fetch('/api/v1/xxx')）：
            # 浏览器端由 shim 改前缀；万一漏了，服务端还会按 Referer 兜底转发。
            # 这里验证的是服务端那层（自检脚本不执行 JS）。
            status, body, _ = client.call(
                "GET", "/api/v1/game-config", timeout=15, headers={"Referer": f"{BASE}/gw/{slug}/"}
            )
            check(
                "根路径接口被兜底转发（带网关 Referer）",
                status == 200 and '"mock-target"' in body,
                f"status={status} {body[:160]}",
            )

            status, body, _ = client.call("GET", "/api/v1/game-config", timeout=10)
            check("无网关 Referer 时门户照常 404", status == 404, f"status={status} {body[:120]}")

            strict_anon = Client(follow_redirects=False)
            status, body, _ = strict_anon.call(
                "GET", "/api/v1/game-config", timeout=10, headers={"Referer": f"{BASE}/gw/{slug}/"}
            )
            check("未登录伪造 Referer 被拒 401", status == 401, f"status={status} {body[:120]}")
        else:
            check("经网关获取内网页面 200", status in (200, 502), f"status={status}")

        if restore_target is not None:
            card_id, original, original_mode = restore_target
            status, text, _ = client.call(
                "PATCH",
                f"/api/cards/{card_id}",
                {"target_url": original, "open_mode": original_mode},
            )
            check("还原卡片原地址与访问方式", status == 200, f"status={status} {text[:160]}")

    print("\n[6] 多环境地址：一张卡片挂多条地址，按环境标识代理")
    # 这里专门建一张临时卡片来验，而不是复用上面的 address-nav：
    # 上面的用例会把它改成 proxy 又还原，混在一起会让"环境地址到底生效没有"看不清。
    # 跑完即删，不给用户留垃圾。
    if not (reachable and password):
        print("  \033[33mSKIP\033[0m  未登录或服务不可达，跳过")
    else:
        mock_for_endpoint = MOCK_TARGET or "http://127.0.0.1:9/"
        status, text, _ = client.call(
            "POST",
            "/api/cards",
            {
                "title": "自检-多环境",
                "slug": "verify-multi",
                "target_url": "http://127.0.0.1:9/",
                "open_mode": "direct",
                "endpoints": [
                    {"name": "prod", "url": mock_for_endpoint, "open_mode": "proxy"},
                    {"name": "dev", "url": "http://127.0.0.1:9/", "open_mode": "direct"},
                ],
            },
        )
        check("建立带 2 条环境地址的卡片", status == 201, f"status={status} {text[:200]}")
        multi_id = 0
        prod_slug = ""
        if status == 201:
            try:
                created = json.loads(text)
            except json.JSONDecodeError:
                created = {}
            multi_id = int(created.get("id") or 0)
            endpoints = created.get("endpoints") or []
            check("环境地址被落库并回带", len(endpoints) == 2, str(endpoints))
            by_name = {item.get("name"): item for item in endpoints}
            prod = by_name.get("prod") or {}
            dev = by_name.get("dev") or {}
            prod_slug = str(prod.get("slug") or "")
            check("ascii 环境名直接用作标识", prod_slug == "verify-multi-prod", prod_slug)
            check(
                "代理环境地址的入口指向网关",
                prod.get("launch_url") == f"/gw/{prod_slug}/",
                str(prod.get("launch_url")),
            )
            check(
                "直连环境地址的入口就是原地址",
                dev.get("launch_url") == "http://127.0.0.1:9/",
                str(dev.get("launch_url")),
            )

        if prod_slug:
            strict = Client(follow_redirects=False, jar=client.jar)
            status, _, headers = strict.call("GET", f"/gw/{prod_slug}", timeout=10)
            check("环境入口 308 补尾斜杠", status == 308, f"status={status}")
            check(
                "308 指向环境标识而不是卡片标识",
                headers.get("Location") == f"/gw/{prod_slug}/",  # type: ignore[union-attr]
                str(headers.get("Location")),  # type: ignore[union-attr]
            )
            status, body, _ = client.call("GET", f"/gw/{prod_slug}/", timeout=15)
            if MOCK_TARGET:
                check("按环境标识代理内网页面成功", status == 200, f"status={status} {body[:200]}")
                check(
                    "注入的 <base> 用的是环境标识",
                    f'<base href="/gw/{prod_slug}/">' in body,
                    body[:400],
                )
                check(
                    "页面里没有漏出卡片标识",
                    "/gw/verify-multi/" not in body,
                    body[:400],
                )
                status, _, _ = client.call("GET", f"/gw/{prod_slug}/static/theme-dark/theme.css", timeout=10)
                check("环境前缀下的静态资源可加载", status == 200, f"status={status}")
            else:
                print("  \033[33mSKIP\033[0m  未设置 DTB_MOCK_TARGET，跳过实际转发（结构已校验）")

            status, body, _ = client.call("GET", "/gw/verify-multi-nope/", timeout=10)
            check("不存在的标识给出友好错误页", "不存在" in body, f"status={status} {body[:160]}")

        # 卡片停用后，它的所有环境入口要一起关掉
        if multi_id:
            status, _, _ = client.call("PATCH", f"/api/cards/{multi_id}", {"enabled": False})
            check("停用临时卡片", status == 200, f"status={status}")
            if prod_slug:
                status, body, _ = client.call("GET", f"/gw/{prod_slug}/", timeout=10)
                check("卡片停用后环境入口一并停用", "停用" in body, f"status={status} {body[:160]}")
            status, _, _ = client.call("DELETE", f"/api/cards/{multi_id}")
            check("清理临时卡片", status == 200, f"status={status}")
        elif multi_id == 0:
            # 建卡失败也要尽量别留垃圾：按标识找回来删掉
            status, body, _ = client.call("GET", "/api/cards?include_disabled=true", timeout=10)
            if status == 200:
                try:
                    stale = [c for c in json.loads(body) if c.get("slug") == "verify-multi"]
                except json.JSONDecodeError:
                    stale = []
                for item in stale:
                    client.call("DELETE", f"/api/cards/{item['id']}")
                    check("清理遗留的临时卡片", True)

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
