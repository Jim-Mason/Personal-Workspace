#!/usr/bin/env python3
"""中台模块内核的端到端自检。

为什么要有这个文件：反代这一层的行为（路径改写、凭据边界、白名单阻断）
全是「对错分明、但坏了很难一眼看出来」的东西 —— 页面上少个前缀，表现是
某个按钮 404，肉眼翻日志要翻很久。所以把判据固化成脚本，改完跑一遍。

跑法（中台要在另一个窗口开着）：
    backend\\scripts\\check_modules.py
或指定地址：
    backend\\scripts\\check_modules.py --base http://127.0.0.1:8731

退出码 0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = ROOT / "data" / ".token"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(state: str, name: str, detail: str = "") -> None:
    results.append((state, name, detail))
    mark = {PASS: "  [OK] ", FAIL: "  [!!] ", SKIP: "  [--] "}[state]
    print(f"{mark}{name}" + (f"  —— {detail}" if detail else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟跳转 —— 重定向本身就是要验的行为之一。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def header_all(headers, name: str) -> list[str]:
    """取同名响应的全部值。

    必须支持一条响应里有多个 `Set-Cookie` —— 用 dict() 收头会把重复项压掉，
    于是「模块自己的 Cookie」和「中台下发的票据 Cookie」只剩最后一个，
    断言就成了假的。
    """
    if hasattr(headers, "get_all"):
        return headers.get_all(name) or []
    return [value for key, value in headers.items() if key.lower() == name.lower()]


def header_one(headers, name: str) -> str:
    values = header_all(headers, name)
    return values[0] if values else ""


def request(
    base: str,
    path: str,
    *,
    token: str = "",
    cookie: str = "",
    method: str = "GET",
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 15.0,
):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method=method, data=data)
    if token:
        req.add_header("X-LocalDeck-Token", token)
    if cookie:
        req.add_header("Cookie", cookie)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - 固定回环地址
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, {}, str(exc).encode()


# 自带登录体系、需要用「零 Cookie 重放」实测会话隔离的模块。
# 加新模块时在这里补一行即可；读不到口令就记为跳过，不算失败。
#
# `local_trust`: 该模块挂在中台后面时是**免登录**的（它自己开了
# TRUST_LOCAL_PROXY）。这类模块的判据要换 —— 见 main() 第 11 节。
_SESSION_PROBES: dict[str, dict[str, str]] = {
    "portal": {
        "login": "/api/auth/login",
        "probe": "/api/auth/me",
        "user": "username",
        "secret": "password",
        "local_trust": "1",
    },
}


def _bootstrap_credentials(module_id: str) -> tuple[str, str] | None:
    """从模块自己的 .env 里取出厂管理员账号口令。

    刻意不从命令行参数要：**口令不写进本脚本**，也不进 shell 历史。
    模块的 .env 本来就是它的配置载体，读它既准又不新增泄露面。
    """
    for candidate in (
        ROOT / "modules" / module_id / ".env",
        ROOT / "modules" / module_id / "backend" / ".env",
    ):
        if not candidate.is_file():
            continue
        values: dict[str, str] = {}
        for raw in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().upper()] = value.strip().strip('"').strip("'")
        user = values.get("BOOTSTRAP_ADMIN_USERNAME", "")
        secret = values.get("BOOTSTRAP_ADMIN_PASSWORD", "")
        if user and secret:
            return user, secret
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8731")
    parser.add_argument("--module", default="opsgen")
    parser.add_argument("--template", default="nginx", help="业务路径检查用的模板名")
    args = parser.parse_args()
    base = args.base
    mid = args.module

    if not TOKEN_PATH.exists():
        print(f"读不到访问令牌：{TOKEN_PATH}（中台至少启动过一次才会生成）")
        return 2
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()

    print("=" * 68)
    print(f" 模块内核自检 —— {base} / 模块 {mid}")
    print("=" * 68)

    # ---------------------------------------------------------- 0. 中台在线
    status, _, body = request(base, "/api/health")
    if status != 200:
        record(FAIL, "中台健康检查", f"HTTP {status} {body[:120]!r}")
        return 1
    record(PASS, "中台健康检查", body.decode("utf-8", "replace")[:80])

    # ---------------------------------------------------------- 1. 模块注册
    status, _, body = request(base, "/api/modules", token=token)
    if status != 200:
        record(FAIL, "读取模块列表", f"HTTP {status}")
        return 1
    listing = json.loads(body)
    record(PASS, "读取模块列表", f"{len(listing['items'])} 个模块，声明错误 {len(listing['errors'])} 个")

    if listing["errors"]:
        for module_id, message in listing["errors"].items():
            record(FAIL, f"模块 {module_id} 的声明", message)

    target = next((item for item in listing["items"] if item["id"] == mid), None)
    if target is None:
        record(FAIL, f"模块 {mid} 已注册", "列表里找不到它")
        return 1

    mount = target["mount"]
    if target["kind"] in {"native", "static"}:
        # native / static 模块**没有自己的进程**，state 恒为 stopped —— 拿它判
        # 「跑没跑」会永远失败。这类模块的等价判据是 proxy_ready（＝已挂载）。
        ok = bool(target.get("proxy_ready"))
        record(
            PASS if ok else FAIL,
            f"模块 {mid} 已挂载（{target['kind_label']}）",
            "同进程挂载 / 托管产物，没有独立进程与端口"
            if ok
            else "没有挂上：可能 enabled=False，或挂载时抛了异常",
        )
        if not ok:
            return 1
    elif target["state"] != "running":
        record(FAIL, f"模块 {mid} 运行状态", f"{target['state']}：{target['error']}")
        print_log_tail(base, token, mid)
        return 1
    else:
        record(
            PASS,
            f"模块 {mid} 运行状态",
            f"PID {target['pid']}" + ("（接管自外部进程）" if target["attached"] else ""),
        )

    # ---------------------------------------------------------- 2. 凭据边界
    status, _, _ = request(base, f"{mount}/")
    record(PASS if status == 401 else FAIL, "裸访问模块路径应被拒", f"HTTP {status}（期望 401）")

    status, _, _ = request(base, f"{mount}/?token={token}")
    record(
        PASS if status == 401 else FAIL,
        "中台令牌不许走 URL 查询串进模块",
        f"HTTP {status}（期望 401）",
    )

    status, headers, _ = request(base, f"{mount}/", token=token)
    record(
        PASS if status == 200 else FAIL,
        "中台令牌走请求头可以进模块",
        f"HTTP {status}（期望 200）",
    )

    # ---------------------------------------------------------- 3. 票据换 Cookie
    #
    # 走**浏览器真实的那条路**：中台在模块面板里给出的入口是 `<mount>/?_t=<票据>`，
    # iframe 加载的就是它。只有用票据进门，中台才会顺手种下长期 Cookie
    # （页面里的 CSS/JS/图片带不上自定义请求头，没有这个 Cookie 全会 401）。
    #
    # 这里刻意不用中台令牌请求头来触发下发：那是中台前端调接口的走法，
    # 浏览器导航带不了自定义头，用它当判据等于在测一条现实中不存在的路径。
    entry = f"{mount}/?_t={target['open_url'].split('_t=')[-1]}"
    status, headers, body = request(base, entry)
    tickets = header_all(headers, "set-cookie")
    cookie = ""
    ticket_flags = ""
    for raw in tickets:
        first = raw.split(";")[0]
        if first.startswith("pw_mod_"):
            cookie = first
            ticket_flags = raw
    if not cookie:
        record(FAIL, "模块票据换 Cookie（从入口地址进入）", f"收到的 Set-Cookie：{tickets}")
        return 1
    record(PASS, "模块票据换 Cookie（从入口地址进入）", cookie.split("=")[0])
    record(
        PASS
        if "httponly" in ticket_flags.lower() and f"path={mount}" in ticket_flags.lower()
        else FAIL,
        "票据 Cookie 是 HttpOnly 且限定在模块路径",
        ticket_flags,
    )

    status, _, body = request(base, f"{mount}/", cookie=cookie)
    record(PASS if status == 200 else FAIL, "票据 Cookie 可以进模块", f"HTTP {status}（期望 200）")

    if mid == "opsgen":
        # ---------------------------------------------------------- 4. 路径改写
        html = body.decode("utf-8", "replace")
        record(
            PASS if "/opsgen/static/css/style.css" in html else FAIL,
            "HTML 里的根路径已补模块前缀",
            "静态样式表" if "/opsgen/static/css/style.css" in html else "没找到 /opsgen/static/css/style.css",
        )
        record(
            PASS if '"/static/' not in html and "'/static/" not in html else FAIL,
            "HTML 里没有残留的裸根路径",
        )
        record(
            PASS if "window.__LOCALDECK__" in html else FAIL,
            "路径垫片已注入",
            "挂载点与实时通道开关会一起下发",
        )
        stripped = "cdnjs" not in html
        record(
            PASS if stripped or target["allow_external_assets"] else FAIL,
            "外部 CDN 资源已剥离",
            "页面里没有第三方域名引用" if stripped else "仍有 cdnjs 引用",
        )

        # ---------------------------------------------------------- 5. 子资源
        status, _, _ = request(base, f"{mount}/static/css/style.css", cookie=cookie)
        record(PASS if status == 200 else FAIL, "子资源（CSS）可加载", f"HTTP {status}")

        status, _, body = request(base, f"{mount}/api/templates", cookie=cookie)
        ok = status == 200 and b'"items"' in body[:400]
        record(PASS if ok else FAIL, "模块 JSON 接口可调用", f"HTTP {status}")

        # ---------------------------------------------------------- 6. 重定向
        status, headers, _ = request(base, mount, cookie=cookie)
        location = header_one(headers, "location")
        ok = status in {301, 302, 307, 308} and location.startswith(f"{mount}/")
        record(
            PASS if ok else FAIL,
            "模块入口补斜杠并保留前缀",
            f"HTTP {status} → {location or '(无 Location)'}",
        )

        # ---------------------------------------------------------- 7. 白名单阻断
        if not target["allow_realtime"]:
            for path in ("/socket.io/?EIO=4&transport=polling", "/socket.io/"):
                status, _, _ = request(base, f"{mount}{path}", cookie=cookie)
                record(
                    PASS if status == 403 else FAIL,
                    f"实时通道被阻断 {path}",
                    f"HTTP {status}（期望 403）",
                )
        else:
            record(SKIP, "实时通道阻断", "allow_realtime=True，本模块显式放行了")

        # ---------------------------------------------------------- 8. 越权与穿越
        status, _, _ = request(base, f"{mount}/../../etc/passwd", cookie=cookie)
        record(
            PASS if status in {400, 404} else FAIL,
            "路径穿越被拒",
            f"HTTP {status}（期望 400/404）",
        )

        status, _, _ = request(base, "/api/apps", cookie=cookie)
        record(
            PASS if status == 401 else FAIL,
            "模块票据不能访问中台 /api/*",
            f"HTTP {status}（期望 401）",
        )

        # ---------------------------------------------------------- 9. 上游未改
        # 判据：上游那个不安全的默认值（host=0.0.0.0）**还在文件里**。
        # 它还在，说明中台确实没有去改上游源码，而是靠自己那层启动壳
        # 接管了监听地址 —— 这正是「第三方代码按不可信对待、且不动其源码」
        # 这条约束的可验证形式。
        app_py = ROOT / "modules" / mid / "app.py"
        if app_py.exists():
            source = app_py.read_text(encoding="utf-8", errors="replace")
            record(
                PASS if 'host="0.0.0.0"' in source else FAIL,
                "上游源码保持原样（中台没改它）",
                'app.py 里仍是上游原来的 host="0.0.0.0"',
            )

        # ---------------------------------------------------------- 10. 业务路径
        # 上面 1~9 节验的是「内核有没有做对它该做的事」，判据都是内核自己产的；
        # 这一节改走**真实用户路径**，因为改写层最容易漏的恰恰是这几处：
        # 表单的 action、重定向的 Location、页面里拼出来的绝对 URL，
        # 以及 —— 最要紧的一条 —— **下载内容有没有被顺手改写**。
        # 该被改写的只有 HTML 和 CSS；一旦把生成的脚本正文也改掉，
        # 用户下载到的就是个坏文件，而且往往要到执行时才炸。
        if mid == "opsgen":
            tpl = args.template
            status, _, body = request(base, f"{mount}/template/{tpl}", cookie=cookie)
            if status != 200:
                record(SKIP, "业务路径检查", f"模板 {tpl} 打不开（HTTP {status}），跳过")
            else:
                form = body.decode("utf-8", "replace")
                record(PASS, f"模板表单可打开 /template/{tpl}", f"HTTP {status}")
                record(
                    PASS if f'action="{mount}/template/{tpl}"' in form else FAIL,
                    "表单 action 已补模块前缀",
                )
                fields = [n for n in re.findall(r'name="([^"]+)"', form) if n]
                record(PASS if fields else FAIL, "表单带出了参数字段", f"{len(fields)} 个：{fields[:4]}")

                # POST 请求体转发 —— 前端 fetch 走的正是这条
                status, _, _ = request(
                    base,
                    f"{mount}/api/favorites/{tpl}",
                    method="POST",
                    data=json.dumps({"name": tpl}).encode(),
                    cookie=cookie,
                    headers={"Content-Type": "application/json"},
                )
                record(PASS if status == 200 else FAIL, "POST 请求体可转发到模块", f"HTTP {status}")

                # 一键生成：这条路径必定产出结果页与分享链接
                status, headers, _ = request(base, f"{mount}/template/{tpl}/quick", cookie=cookie)
                location = header_one(headers, "location")
                ok = status in {301, 302, 303, 307} and location.startswith(f"{mount}/result/")
                record(
                    PASS if ok else FAIL,
                    "一键生成的重定向保留前缀",
                    f"HTTP {status} → {location or '(无 Location)'}",
                )
                if ok:
                    status, _, body = request(base, location, cookie=cookie)
                    page = body.decode("utf-8", "replace")
                    record(PASS if status == 200 else FAIL, "结果页可打开", f"HTTP {status}")
                    record(
                        PASS if re.search(rf"{re.escape(base)}{mount}/share/[0-9a-f]+", page) else FAIL,
                        "页面里拼出的分享链接已换成中台地址",
                        "上游用的是 request.url_root，由改写层负责替换",
                    )
                    record(
                        PASS if str(target["internal_port"]) not in page else FAIL,
                        "结果页不泄露模块的内部端口",
                        f"内部端口 {target['internal_port']}",
                    )

                    link = re.search(rf'href="({mount}/download/[^"]+)"', page)
                    if not link:
                        record(SKIP, "下载单个脚本", "结果页里没有下载链接")
                    else:
                        path = link.group(1)
                        status, _, served = request(base, path, cookie=cookie)
                        record(
                            PASS if status == 200 and served else FAIL,
                            "下载单个脚本",
                            f"HTTP {status} / {len(served)} 字节",
                        )
                        record(
                            PASS if not served.lstrip()[:1] == b"<" else FAIL,
                            "下载内容没被当成 HTML 改写",
                            f"响应体开头 {served[:14]!r}",
                        )
                        # 最硬的判据：经代理拿到的字节，与绕过中台直连模块拿到的
                        # 字节**逐个相同**。不同就说明改写层伸手伸到了不该伸的地方。
                        port = target.get("internal_port")
                        if port:
                            raw_status, _, raw = request(
                                f"http://127.0.0.1:{port}", path[len(mount):], cookie=cookie
                            )
                            if raw_status == 200 and raw:
                                record(
                                    PASS if raw == served else FAIL,
                                    "代理字节 == 模块直出字节（逐字节相同）",
                                    f"{len(served)} 字节" if raw == served else f"直连 {len(raw)} / 代理 {len(served)}",
                                )
                            else:
                                record(SKIP, "代理字节对照", f"直连模块拿不到（HTTP {raw_status}）")

    elif mid == "portal":
        # 模块三是一个 SPA + 自带网关的完整应用，判据与 opsgen 那套完全不同：
        # 它有构建产物、前端路由，还有一条属于自己的反向代理链路。
        html = body.decode("utf-8", "replace")
        asset = re.search(r'src="([^"]*/assets/[^"]+\.js)"', html)
        record(
            PASS if asset and asset.group(1).startswith(f"{mount}/assets/") else FAIL,
            "SPA 首页的资源引用已补模块前缀",
            asset.group(1) if asset else "首页里没找到入口 JS",
        )
        record(
            PASS if "window.__LOCALDECK__" in html else FAIL,
            "路径垫片已注入",
            "前端要用它算路由 basename",
        )
        record(
            PASS if '"/assets/' not in html else FAIL,
            "首页里没有残留的裸根路径",
        )

        # 登录一次，把模块自己的会话**主动**带上。这与第 11 节要防的
        # 「服务端替我们带上」是两回事，两者必须能同时成立。
        authed = cookie
        creds = _bootstrap_credentials(mid)
        if creds:
            user, secret = creds
            status, headers, _ = request(
                base,
                f"{mount}/api/auth/login",
                cookie=cookie,
                method="POST",
                data=json.dumps({"username": user, "password": secret}).encode(),
                headers={"Content-Type": "application/json"},
            )
            if status == 200:
                for raw in header_all(headers, "set-cookie"):
                    if raw.startswith("dtb_access="):
                        authed = f"{cookie}; {raw.split(';')[0]}"
            record(
                PASS if status == 200 else FAIL,
                "模块自带登录接口可经代理调用",
                f"HTTP {status}",
            )
        else:
            record(SKIP, "模块自带登录接口可经代理调用", "读不到出厂口令")

        if asset:
            js_path = asset.group(1)
            status_js, _, js = request(base, js_path, cookie=authed)
            record(
                PASS if status_js == 200 else FAIL,
                "SPA 入口 JS 可加载",
                f"HTTP {status_js} / {len(js)} 字节",
            )
            # basename 的有效性只能从产物里读：源码里写了不算数，得看构建
            # 产物是否真的会去读中台注入的挂载点。读到旧构建就会是空页面。
            record(
                PASS if b"__LOCALDECK__" in js else FAIL,
                "构建产物会读 __LOCALDECK__.mount（basename 生效）",
                "读不到说明停在旧构建上，需要重新 npm run build",
            )
            port = target.get("internal_port")
            if port and status_js == 200:
                raw_status, _, raw = request(
                    f"http://127.0.0.1:{port}", js_path[len(mount):], cookie=authed
                )
                if raw_status == 200 and raw:
                    record(
                        PASS if raw == js else FAIL,
                        "代理字节 == 模块直出字节（逐字节相同）",
                        f"{len(js)} 字节"
                        if raw == js
                        else f"直连 {len(raw)} / 代理 {len(js)}",
                    )
                else:
                    record(SKIP, "代理字节对照", f"直连模块拿不到（HTTP {raw_status}）")

        # SPA 深链接：前端路由 /admin 这类地址刷新时必须回落到首页，
        # 否则用户一按 F5 就是 404。
        status, _, deep = request(base, f"{mount}/admin", cookie=authed)
        record(
            PASS if status == 200 and b'id="root"' in deep else FAIL,
            "SPA 深链接回落到首页",
            f"{mount}/admin → HTTP {status}",
        )

        # 模块自带网关：它把内网页面代理进来看起来像自家的子路径，
        # 这条链路是「中台 → 模块 → 内网」的第二层代理。
        status, headers, _ = request(base, f"{mount}/gw/address-nav", cookie=authed)
        location = header_one(headers, "location")
        record(
            PASS if status in {307, 308} and location.startswith(f"{mount}/gw/") else FAIL,
            "网关补尾斜杠时保留模块前缀",
            f"HTTP {status} → {location or '(无 Location)'}",
        )
        gw_path = location or f"{mount}/gw/address-nav/"
        status, _, gw = request(base, gw_path, cookie=authed, timeout=30.0)
        record(
            PASS if status == 200 else FAIL,
            "内网页面经两层代理可取回",
            f"{gw_path} → HTTP {status} / {len(gw)} 字节",
        )
        if status == 200:
            page = gw.decode("utf-8", "replace")
            record(
                PASS
                if f'data-dtb-gateway="{gw_path}"' in page
                else FAIL,
                "网关注入的基路径带模块前缀",
                "被代理页面里的相对路径靠它决定往哪走",
            )

    elif mid == "filelist":
        # 模块四是「自带页面的 native 模块」：路由直接进中台的应用，页面与静态
        # 资源由模块自己托管。判据围绕三件事 —— 页面能开、资源是真的、
        # **越界必须被拒**（这条最要紧，它是这个模块唯一的安全阀）。
        html = body.decode("utf-8", "replace")
        record(
            PASS if "{{MOUNT}}" not in html else FAIL,
            "页面里的挂载点占位符已替换",
            "残留占位符会让资源整片 404",
        )
        asset = re.search(rf'(?:href|src)="({re.escape(mount)}/assets/[^"]+)"', html)
        record(
            PASS if asset else FAIL,
            "页面引用的资源带模块前缀",
            asset.group(1) if asset else "首页里没找到带前缀的资源",
        )
        if asset:
            asset_path = asset.group(1)
            status, _, served = request(base, asset_path, cookie=cookie)
            record(
                PASS if status == 200 and served else FAIL,
                "自带资源可加载",
                f"{asset_path} → HTTP {status} / {len(served)} 字节",
            )
            local = ROOT / "modules" / mid / "web" / asset_path.rsplit("/", 1)[-1]
            if local.is_file():
                same = served == local.read_bytes()
                record(
                    PASS if same else FAIL,
                    "资源字节 == 模块磁盘原文（逐字节相同）",
                    f"{len(served)} 字节"
                    if same
                    else f"磁盘 {local.stat().st_size} / 代理 {len(served)}",
                )
            else:
                record(SKIP, "资源字节对照", f"找不到磁盘文件 {local}")

        status, _, body = request(base, f"{mount}/api/roots", cookie=cookie)
        try:
            roots = json.loads(body or b"{}")
        except ValueError:
            roots = {}
        items = roots.get("items") if isinstance(roots, dict) else None
        record(
            PASS if status == 200 and isinstance(items, list) else FAIL,
            "书库列表接口可用",
            f"HTTP {status} / {len(items or [])} 个书库",
        )

        root_id = (items or [{}])[0].get("id", "") if items else ""
        if not root_id:
            record(SKIP, "列一层目录", "还没有登记书库，无法继续")
            record(SKIP, "越界路径一律被拒", "还没有登记书库，无法继续")
        else:
            status, _, body = request(
                base, f"{mount}/api/entries?root={root_id}&path=", cookie=cookie
            )
            try:
                listing = json.loads(body or b"{}")
            except ValueError:
                listing = {}
            record(
                PASS if status == 200 and "entries" in listing else FAIL,
                "列一层目录",
                f"HTTP {status} / {listing.get('counts')}",
            )

            # ⛔ 只读边界：越界路径一个都不能漏。这几个探针覆盖三种绕过思路 ——
            # 相对回溯、盘符绝对路径、POSIX 绝对路径，正反斜杠各来一遍。
            probes = ["..", "../..", "sub/../../..", "C:\\Windows", "/Windows", "..\\..\\Windows"]
            leaked = []
            for probe in probes:
                code, _, _ = request(
                    base,
                    f"{mount}/api/entries?root={root_id}&path={quote(probe, safe='')}",
                    cookie=cookie,
                )
                if code != 400:
                    leaked.append(f"{probe}→HTTP {code}")
            record(
                PASS if not leaked else FAIL,
                "越界路径一律被拒（.. 与绝对路径）",
                f"{len(probes)} 个探针全部 400"
                if not leaked
                else "有漏网的：" + "、".join(leaked),
            )

    else:
        record(
            SKIP,
            "第 4–10 节（业务路径与改写）",
            "这一组是按模块写的具体用例：opsgen 与 portal 已有，其它模块跳过",
        )

    # ------------------------------------- 11. 会话隔离（服务端不得记住模块会话）
    #
    # 这一节源于一次真实越权：中台代理用的是**一个长期存活、被所有模块共用**
    # 的 httpx 客户端，而 httpx 默认会在每个响应到达时把 Set-Cookie 存进自己的
    # Cookie 罐，之后自动合并进后续出站请求。Cookie 的作用域只看域名、**不看
    # 端口**，于是「谁在 A 模块登录过一次」就变成「此后所有模块的请求都带着
    # 那份会话」—— 实测零 Cookie 访问 /portal/api/auth/me 曾返回 200 + 管理员。
    # 下面一条查机制、一条查实况，缺一不可。
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        import httpx

        from app.services.modules import proxy as proxy_module

        probe_client = httpx.AsyncClient()
        proxy_module._disable_cookie_jar(probe_client)
        # 这里只是喂一个假的响应给 Cookie 罐，**不会真的发请求**，所以端口填什么
        # 都行。native / static 模块没有 internal_port（它们是同进程挂载），
        # 拿它拼 URL 会拼出 "Invalid port: 'None'"。
        probe_client.cookies.extract_cookies(
            httpx.Response(
                200,
                headers=[("set-cookie", "planted_session=LEAK; Path=/; HttpOnly")],
                request=httpx.Request("GET", "http://127.0.0.1:1/"),
            )
        )
        merged = str(probe_client._merge_cookies(None))
        clean = len(probe_client.cookies) == 0 and "LEAK" not in merged
        record(
            PASS if clean else FAIL,
            "代理客户端已关闭 Cookie 罐（不记忆模块会话）",
            "罐为空，出站不会带残留会话"
            if clean
            else f"罐里仍有 {len(probe_client.cookies)} 条，出站会带：{merged[:80]}",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            FAIL,
            "代理客户端已关闭 Cookie 罐（不记忆模块会话）",
            f"{type(exc).__name__}: {exc}",
        )

    plan = _SESSION_PROBES.get(mid)
    creds = _bootstrap_credentials(mid) if plan else None
    if not plan or creds is None:
        record(
            SKIP,
            "零 Cookie 重放不应通过鉴权",
            "该模块没有登记登录用例，或读不到它的出厂口令",
        )
    elif plan.get("local_trust"):
        # 这个模块挂在中台后面是**有意免登录**的，所以「零 Cookie 重放必须 401」
        # 在它身上不成立 —— 那不是漏洞，是需求。换成真正该守住的那一条：
        # **免登录的正当性完全来自上游那道边界，边界不在就不能免**。
        # 绕过中台、直连它的内部端口，必须还是 401。
        port = target.get("internal_port")
        # 带的是**模块票据 Cookie**（第 3 节换来的那个），也就是浏览器进入
        # 模块后手里实际有的东西 —— 不是中台令牌，也不是门户自己的会话。
        status, _, body = request(base, f"{mount}{plan['probe']}", cookie=cookie)
        record(
            PASS if status == 200 else FAIL,
            "经中台访问免登录生效",
            f"{plan['probe']} → HTTP {status}（期望 200）"
            + (f"，响应体 {body[:60]!r}" if status != 200 else ""),
        )
        if not port:
            record(SKIP, "免登录只在经中台时生效", "该模块没有内部端口（同进程挂载）")
        else:
            direct_base = f"http://127.0.0.1:{port}"
            status, _, _ = request(direct_base, plan["probe"])
            record(
                PASS if status == 401 else FAIL,
                "免登录只在经中台时生效（直连内部端口仍 401）",
                f"直连 {direct_base}{plan['probe']} → HTTP {status}（期望 401）",
            )
    else:
        user, secret = creds
        payload = json.dumps({plan["user"]: user, plan["secret"]: secret}).encode()
        # 前置状态：先让模块**真的发一次会话**。这一步拿不到 200，后面的 401
        # 就什么都证明不了 —— 缺口本该在这里被抓住，而不是靠事后解释。
        status, _, _ = request(
            base,
            f"{mount}{plan['login']}",
            cookie=cookie,
            method="POST",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        record(
            PASS if status == 200 else FAIL,
            "零 Cookie 重放的前置：模块确实发出过会话",
            f"登录 HTTP {status}（期望 200）",
        )
        if status == 200:
            status, _, body = request(base, f"{mount}{plan['probe']}", cookie=cookie)
            record(
                PASS if status == 401 else FAIL,
                "零 Cookie 重放不应通过鉴权",
                f"{plan['probe']} → HTTP {status}（期望 401）"
                + (f"，响应体 {body[:60]!r}" if status == 200 else ""),
            )

    # ---------------------------------------------------------- 汇总
    failed = [item for item in results if item[0] == FAIL]
    print("-" * 68)
    print(
        f" 合计 {len(results)} 项：通过 {sum(1 for r in results if r[0] == PASS)}，"
        f"失败 {len(failed)}，跳过 {sum(1 for r in results if r[0] == SKIP)}"
    )
    if failed:
        print(" 失败明细：")
        for _, name, detail in failed:
            print(f"   - {name}：{detail}")
    print("=" * 68)
    return 1 if failed else 0


def print_log_tail(base: str, token: str, module_id: str) -> None:
    status, _, body = request(base, f"/api/modules/{module_id}/log?lines=30", token=token)
    if status != 200:
        print("   （取不到模块日志）")
        return
    print("   模块日志尾部：")
    for line in json.loads(body)["lines"][-20:]:
        print(f"     | {line}")


if __name__ == "__main__":
    sys.exit(main())
