"""鉴权边界自检：中台令牌 / 浏览器会话 / 模块票据 三条通道。

三个自检脚本的分工（别混着用）：

| 脚本 | 查什么 | 要不要服务在跑 |
|---|---|---|
| `check_modules.py` | 模块内核端到端：凭据边界、路径改写、越界拒绝 | **要**（连真服务） |
| `check_docs.py` | 以代码为事实来源核对手册 | 不要 |
| `check_auth.py` | 鉴权三条通道的边界（本文件） | **不要** |

为什么鉴权这块要单独一个脚本、不并进 `check_modules.py`：

模块的内部端口（8732/8733…）是**模块自己在 module.py 里声明的固定值**，
所以本机上没法同时跑第二个实例来做干净环境的检查 —— 第二个实例要么
撞端口失败，要么被 `_attach_or_fail` 接管到第一个实例的进程上，
于是所有"跨实例"的请求都会拿到**另一个实例的票据**，检查结果毫无意义
（表现还特别像真 bug：portal 会报一个假的「免登录失效」）。

这里用 `TestClient` 并且**刻意不进入 with 块**（进 with 会跑 lifespan，
那就去启动子进程模块了）。native 模块同进程挂载，压根不需要端口，
所以这个脚本随时能跑，也不会打扰正在运行的那个实例。

用法：
    python backend/scripts/check_auth.py
    python backend/scripts/check_auth.py --log            # 顺带查日志文件
    python backend/scripts/check_auth.py --data-dir <路径>  # 换数据目录
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(state: str, name: str, detail: str = "") -> None:
    results.append((state, name, detail))
    mark = {PASS: "  [OK] ", FAIL: "  [!!] ", SKIP: "  [--] "}[state]
    print(f"{mark}{name}" + (f"  —— {detail}" if detail else ""))


# native 模块的样品：中台自己写的模块，凭会话就能访问。
NATIVE_PROBES = (
    ("inventory", "/inventory/api/apps"),
    ("filelist", "/filelist/api/roots"),
)


def check_channels(client, token: str, session_cookie: dict) -> None:
    """中台自己的接口：令牌请求头 与 浏览器会话，任一成立即放行。"""
    print("· 中台接口")
    record(
        PASS if client.get("/api/overview").status_code == 401 else FAIL,
        "无凭据被拒",
    )
    record(
        PASS if client.get("/api/overview", headers={"X-LocalDeck-Token": token}).status_code == 200 else FAIL,
        "令牌请求头放行（脚本 / curl 走这条）",
    )
    record(
        PASS if client.get("/api/overview", cookies=session_cookie).status_code == 200 else FAIL,
        "会话 Cookie 放行（浏览器走这条）",
    )
    record(
        PASS if client.get("/api/overview", cookies={"pw_session": "forged-value"}).status_code == 401 else FAIL,
        "伪造会话被拒",
    )
    record(
        PASS
        if client.get(
            "/api/overview", cookies=session_cookie, headers={"X-LocalDeck-Token": "wrong"}
        ).status_code
        == 200
        else FAIL,
        "会话对 + 令牌头错仍放行（两条通道互不干扰）",
    )


def check_native(client, hub, token: str, session_cookie: dict) -> None:
    """native 模块路径：会话可用（中台自己写的页面要能读自己的数据），票据照旧。"""
    print("· native 模块（中台自己的代码，认会话）")
    from app.security import TICKET_PARAM  # noqa: PLC0415 - 只在这个分支需要

    for module_id, path in NATIVE_PROBES:
        cred = hub.ticket_lookup(path)
        if not cred.ticket:
            record(SKIP, f"{module_id} 的模块凭据", "未挂载或没有 runtime")
            continue
        record(PASS if cred.native else FAIL, f"{module_id} 被识别为 native", f"mount={cred.mount}")

        r = client.get(path, cookies=session_cookie)
        record(PASS if r.status_code in (200, 404) else FAIL, f"{module_id} 会话可用", f"HTTP {r.status_code}")
        record(PASS if client.get(path).status_code == 401 else FAIL, f"{module_id} 无凭据被拒")
        record(
            PASS if client.get(path, headers={"X-LocalDeck-Token": token}).status_code in (200, 404) else FAIL,
            f"{module_id} 令牌头可用",
        )
        # 查询串里的中台令牌会落进 location.search，而模块页面是第三方可读的
        record(
            PASS if client.get(path, params={"token": token}).status_code == 401 else FAIL,
            f"{module_id} 不吃查询串里的中台令牌",
        )

        r = client.get(path, params={TICKET_PARAM: cred.ticket})
        set_cookie = r.headers.get("set-cookie", "")
        record(PASS if r.status_code in (200, 404) else FAIL, f"{module_id} 票据查询参数进门", f"HTTP {r.status_code}")
        record(
            PASS if cred.cookie_name in set_cookie and "HttpOnly" in set_cookie else FAIL,
            f"{module_id} 进门外加种 HttpOnly 模块 Cookie",
        )
        record(
            PASS if client.get(path, cookies={cred.cookie_name: cred.ticket}).status_code in (200, 404) else FAIL,
            f"{module_id} 模块 Cookie 后续免票据",
        )
        record(PASS if client.get(path, params={TICKET_PARAM: "wrong"}).status_code == 401 else FAIL, f"{module_id} 错误票据被拒")

    # 票据不跨模块：A 模块的 Cookie 进不了 B 模块。
    #
    # 这条**必须换一个干净的 client**：TestClient 自带 cookie jar，
    # 上面那轮「票据进门」已经让它把 pw_mod_inventory / pw_mod_filelist
    # 都存下来了 —— 继续用同一个 client，请求会自带合法票据，
    # 于是这条判据永远失败，而看着像"隔离失效了"这种大问题。
    first_cred = hub.ticket_lookup(NATIVE_PROBES[0][1])
    second_cred = hub.ticket_lookup(NATIVE_PROBES[1][1])
    if first_cred.ticket and second_cred.ticket and first_cred.cookie_name != second_cred.cookie_name:
        from starlette.testclient import TestClient  # noqa: PLC0415

        from app.main import app as _app  # noqa: PLC0415

        fresh = TestClient(_app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        r = fresh.get(NATIVE_PROBES[1][1], cookies={first_cred.cookie_name: first_cred.ticket})
        record(
            PASS if r.status_code == 401 else FAIL,
            "票据不跨模块（A 的 Cookie 进不了 B）",
            f"HTTP {r.status_code}",
        )


def check_third_party(token: str, session_cookie: dict) -> None:
    """第三方模块路径：会话一律不认。

    这段必须在**独立的 app** 上做：第三方模块的 runtime 由 lifespan 创建，
    而这个脚本刻意不跑 lifespan。把判定逻辑单独隔离出来，反而测得干净。
    """
    print("· 第三方模块（装着别人的代码，会话一律不认）")
    from fastapi import FastAPI  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    from app.security import ModuleCredential, install_security  # noqa: PLC0415
    from app.services import session as sessions  # noqa: PLC0415

    fake = ModuleCredential(ticket="third-party-ticket", cookie_name="pw_mod_third", mount="/third", native=False)
    probe = FastAPI()

    @probe.get("/third/api/data")
    def third_data():  # noqa: ANN202 - 探针用的假端点
        return {"ok": True}

    @probe.get("/api/overview")
    def probe_overview():  # noqa: ANN202 - 对照组
        return {"ok": True}

    install_security(
        probe,
        token,
        ticket_lookup=lambda path: fake if path.startswith("/third") else ModuleCredential(),
        session_verify=lambda request: sessions.verify(request.cookies.get("pw_session", ""), token),
    )
    client = TestClient(probe, base_url="http://127.0.0.1", raise_server_exceptions=False)

    record(
        PASS if client.get("/third/api/data", cookies=session_cookie).status_code == 401 else FAIL,
        "第三方模块路径 + 有效会话 -> 401（第三方 JS 借不到浏览器的手）",
    )
    record(
        PASS if client.get("/third/api/data", headers={"X-LocalDeck-Token": token}).status_code == 200 else FAIL,
        "第三方模块路径 + 令牌头 -> 放行",
    )
    record(
        PASS if client.get("/third/api/data", params={"_t": "third-party-ticket"}).status_code == 200 else FAIL,
        "第三方模块路径 + 模块票据 -> 放行",
    )
    record(
        PASS if client.get("/api/overview", cookies=session_cookie).status_code == 200 else FAIL,
        "对照组：同一 app 上会话对中台接口有效",
    )


def check_log(data_dir: Path, token: str, session_value: str) -> None:
    """日志：该有的都有，不该有的都没有。"""
    print("· 日志")
    log_path = data_dir / "logs" / "localdeck.log"
    if not log_path.exists():
        record(SKIP, "日志文件", f"{log_path} 还不存在（服务至少完整跑过一次才会有）")
        return
    text = log_path.read_text(encoding="utf-8", errors="replace")
    record(PASS, "日志文件存在", str(log_path))

    missing = [
        label
        for label, needle in (
            ("启动信息", "启动中台"),
            ("请求记录", " GET /"),
            ("401 的拒绝原因", "401 "),
        )
        if needle not in text
    ]
    if missing:
        record(FAIL, "日志内容齐全", f"缺：{'、'.join(missing)}")
    else:
        record(PASS, "日志内容齐全（启动 / 请求 / 拒绝原因都在）")

    # 底线：日志是明文躺在磁盘上的，凭据本体绝不能进去
    if token and token in text:
        record(FAIL, "日志不含令牌明文", "发现了访问令牌，必须修")
    else:
        record(PASS, "日志不含令牌明文")
    if session_value and session_value in text:
        record(FAIL, "日志不含会话号明文", "发现了会话号，必须修")
    else:
        record(PASS, "日志不含会话号明文")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--log", action="store_true", help="顺带检查日志文件")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    sys.path.insert(0, str(ROOT / "backend"))

    from starlette.testclient import TestClient  # noqa: PLC0415

    from app import db  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415
    from app.services import session as sessions  # noqa: PLC0415

    db.run_migrations()

    print("=" * 68)
    print(" 鉴权边界自检 —— 中台令牌 / 浏览器会话 / 模块票据")
    print("=" * 68)

    token = app.state.token
    hub = app.state.hub
    # base_url 必须给回环地址：TestClient 默认 Host 是 "testserver"，
    # 而 Host 校验只认 127.0.0.1/localhost/::1 —— 不给就一路 400，
    # 那看着像"全线失败"，其实是防线本身把它挡了。
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    sid = sessions.create(token, user_agent="check_auth")
    session_cookie = {"pw_session": sid}

    try:
        check_channels(client, token, session_cookie)
        print()
        check_native(client, hub, token, session_cookie)
        print()
        check_third_party(token, session_cookie)
    finally:
        # 探针自己造的会话用完就销 —— 别在用户的会话列表里留垃圾
        sessions.revoke(sid)

    if args.log:
        print()
        check_log(data_dir, token, sid)

    failed = [r for r in results if r[0] == FAIL]
    skipped = [r for r in results if r[0] == SKIP]
    total = len(results)

    print()
    print("=" * 68)
    print(
        f" 合计 {total} 项：通过 {total - len(failed) - len(skipped)}，"
        f"失败 {len(failed)}，跳过 {len(skipped)}"
    )
    if failed:
        print("-" * 68)
        for _, name, detail in failed:
            print(f"  [!!] {name}" + (f"  —— {detail}" if detail else ""))
    print("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
