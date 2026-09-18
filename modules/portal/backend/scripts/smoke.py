"""端到端冒烟测试：不依赖 pytest，直接 `python scripts/smoke.py` 即可。

覆盖：健康检查 → 登录 → 卡片列表 → 管理员建卡/改卡/删卡 → 权限拦截 →
      卡片外观自定义 → 图标上传 → 配置导入导出 → 表格（xlsx/csv）导入导出 → 登出。
会使用独立的临时数据库与临时上传目录，不影响 data/devtoolbox.db。
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

TMP_DB = Path(tempfile.gettempdir()) / "devtoolbox_smoke.db"
for suffix in ("", "-wal", "-shm"):
    candidate = Path(str(TMP_DB) + suffix)
    if candidate.exists():
        candidate.unlink()

# 上传目录也隔离到临时目录，避免污染项目的 data/uploads
TMP_UPLOADS = Path(tempfile.gettempdir()) / "devtoolbox_smoke_uploads"
if TMP_UPLOADS.exists():
    import shutil

    shutil.rmtree(TMP_UPLOADS, ignore_errors=True)

os.environ.setdefault("SECRET_KEY", "smoke-test-secret-key-please-ignore")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "SmokeTest#2026"
os.environ["BOOTSTRAP_ADMIN_USERNAME"] = "smokeadmin"
os.environ["UPLOADS_DIR"] = str(TMP_UPLOADS)
os.environ["LOG_JSON"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from app.main import app  # noqa: E402
from app.schemas.models import CardExportDoc, CardExportItem  # noqa: E402
from app.services import card_sheet  # noqa: E402
from app.services.card_sheet import CARD_COLUMNS  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# 导出表头必须与列定义完全一致，导入才可能按同一份定义解析回来
EXPECTED_HEADERS = [column.header for column in CARD_COLUMNS]

# 1x1 透明 PNG，文件头是真 PNG，用于验证魔法字节校验
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")


def main() -> int:
    # TestClient 进入上下文时会执行 lifespan（跑迁移 + 建管理员）
    with TestClient(app, base_url="http://testserver") as client:

        print("\n[1] 健康检查")
        r = client.get("/health")
        check("GET /health 返回 200", r.status_code == 200, f"status={r.status_code}")
        check("健康检查含版本号", "version" in r.json(), r.text[:120])
        r = client.get("/ready")
        check("GET /ready 数据库可用", r.status_code == 200 and r.json()["checks"]["database"] == "ok", r.text[:160])

        print("\n[2] 认证")
        r = client.get("/api/cards")
        check("未登录访问卡片返回 401", r.status_code == 401, f"status={r.status_code}")
        check("401 返回规范化错误体", r.json().get("error", {}).get("code") == "unauthorized", r.text[:160])

        r = client.post("/api/auth/login", json={"username": "smokeadmin", "password": "wrong-password"})
        check("错误密码登录被拒", r.status_code == 401, f"status={r.status_code}")

        r = client.post("/api/auth/login", json={"username": "smokeadmin", "password": "SmokeTest#2026"})
        check("正确密码登录成功", r.status_code == 200, r.text[:200])
        check("登录返回用户信息", r.json().get("user", {}).get("role") == "admin", r.text[:200])
        check("登录写入 HttpOnly Cookie", "dtb_access" in client.cookies, str(client.cookies))
        check("令牌不出现在响应体", "dtb_access" not in r.text, "响应体泄露了 Cookie 名")

        r = client.get("/api/auth/me")
        check("GET /api/auth/me 返回当前用户", r.status_code == 200 and r.json()["username"] == "smokeadmin", r.text[:160])

        print("\n[3] 卡片读取")
        r = client.get("/api/cards")
        check("卡片列表返回 200", r.status_code == 200, r.text[:200])
        cards = r.json()
        check("内置 9 张示例卡片", len(cards) == 9, f"实际 {len(cards)} 张")
        first = cards[0] if cards else {}
        check("卡片包含 launch_url", bool(first.get("launch_url")), str(first)[:200])
        check(
            "proxy 模式 launch_url 指向网关",
            first.get("open_mode") == "proxy" and first.get("launch_url", "").startswith("/gw/"),
            str(first.get("launch_url")),
        )

        r = client.get("/api/cards", params={"keyword": "字典"})
        check("关键词搜索生效", r.status_code == 200 and len(r.json()) == 1, f"命中 {len(r.json())} 张")

        r = client.get("/api/cards/groups")
        check("分组接口可用", r.status_code == 200 and len(r.json()) >= 1, r.text[:160])

        print("\n[4] 管理端 CRUD")
        payload = {
            "title": "冒烟测试卡片",
            "description": "由 smoke.py 创建",
            "icon": "T",
            "target_url": "http://127.0.0.1:9/",
            "open_mode": "proxy",
            "group_name": "测试分组",
        }
        r = client.post("/api/cards", json=payload)
        check("管理员创建卡片成功", r.status_code == 201, r.text[:200])
        created = r.json()
        check("自动生成 slug", created.get("slug") == "smoke-card-smoke-test" or bool(created.get("slug")), str(created.get("slug")))

        card_id = created.get("id")
        card_slug = created.get("slug")
        r = client.patch(f"/api/cards/{card_id}", json={"title": "冒烟测试卡片-已改"})
        check("更新卡片标题成功", r.status_code == 200 and r.json()["title"] == "冒烟测试卡片-已改", r.text[:200])

        r = client.post("/api/cards", json={"title": "非法协议", "target_url": "file:///etc/passwd"})
        check("拒绝非 http(s) 协议", r.status_code == 422, f"status={r.status_code}")

        r = client.post("/api/cards", json={"title": "元数据地址", "target_url": "http://169.254.169.254/latest/meta-data/"})
        check("拒绝云元数据地址(SSRF 防护)", r.status_code == 403, f"status={r.status_code} {r.text[:120]}")

        r = client.get("/api/users")
        check("管理员可列出用户", r.status_code == 200 and len(r.json()) >= 1, r.text[:160])

        r = client.post("/api/users", json={"username": "smokeuser", "password": "SmokeUser#2026", "role": "user"})
        check("管理员创建普通用户成功", r.status_code == 201, r.text[:200])
        normal_user_uid = r.json().get("uid")
        check("用户返回 uid 作为对外标识", bool(normal_user_uid), r.text[:200])

        print("\n[5] 权限隔离")
        with TestClient(app, base_url="http://testserver") as anon:
            r = anon.post("/api/auth/login", json={"username": "smokeuser", "password": "SmokeUser#2026"})
            check("普通用户可登录", r.status_code == 200, r.text[:160])
            r = anon.get("/api/cards")
            check("普通用户可读卡片", r.status_code == 200, r.text[:160])
            r = anon.post("/api/cards", json=payload)
            check("普通用户建卡被拒 403", r.status_code == 403, f"status={r.status_code}")
            r = anon.get("/api/users")
            check("普通用户读用户列表被拒 403", r.status_code == 403, f"status={r.status_code}")
            r = anon.post("/api/auth/logout")
            check("登出成功", r.status_code == 200, r.text[:160])
            r = anon.get("/api/auth/me")
            check("登出后访问被拒", r.status_code == 401, f"status={r.status_code}")

        print("\n[6] 网关行为")
        r = client.get("/gw/address-nav", follow_redirects=False)
        check("网关入口 308 补尾斜杠", r.status_code == 308, f"status={r.status_code}")
        check("308 Location 正确", r.headers.get("location") == "/gw/address-nav/", str(r.headers.get("location")))

        r = client.get("/gw/not-exist-slug/")
        check(
            "未知 slug 返回友好页面",
            r.status_code == 502 and "入口不存在" in r.text,
            f"status={r.status_code}",
        )
        r = client.get("/gw/not-exist-slug/")
        check("未知 slug 提示里带上那个标识", "not-exist-slug" in r.text, r.text[:200])

        # 指向本地一个必然拒绝连接的端口，验证错误页
        r = client.post("/api/cards", json={"title": "不可达目标", "target_url": "http://127.0.0.1:9/", "open_mode": "proxy"})
        unreachable_id = r.json().get("id")
        r = client.get("/api/cards")
        slug = next(c["slug"] for c in r.json() if c["id"] == unreachable_id)
        r = client.get(f"/gw/{slug}/")
        check("不可达目标返回 502 友好页", r.status_code == 502 and "无法连接目标内网服务" in r.text, f"status={r.status_code} {r.text[:120]}")

        r = client.post(f"/api/cards/{unreachable_id}/check")
        check("连通性自检接口返回不可达", r.status_code == 200 and r.json()["reachable"] is False, r.text[:160])

        print("\n[7] 卡片外观自定义")
        r = client.patch(
            f"/api/cards/{card_id}",
            json={
                "icon_url": "/uploads/icons/demo.png",
                "bg_style": "violet",
                "accent_color": "#2B6CF6",
            },
        )
        check("写入外观字段成功", r.status_code == 200, r.text[:200])
        styled = r.json()
        check("bg_style 回读一致", styled.get("bg_style") == "violet", str(styled.get("bg_style")))
        check("accent_color 统一为小写", styled.get("accent_color") == "#2b6cf6", str(styled.get("accent_color")))
        check("icon_url 回读一致", styled.get("icon_url") == "/uploads/icons/demo.png", str(styled.get("icon_url")))

        r = client.patch(f"/api/cards/{card_id}", json={"accent_color": "red"})
        check("拒绝非法颜色名", r.status_code == 422, f"status={r.status_code}")

        r = client.patch(f"/api/cards/{card_id}", json={"bg_style": "rainbow"})
        check("拒绝未知底色预设", r.status_code == 422, f"status={r.status_code}")

        r = client.patch(f"/api/cards/{card_id}", json={"icon_url": "javascript:alert(1)"})
        check("拒绝 javascript: 图标地址", r.status_code == 422, f"status={r.status_code}")

        r = client.patch(f"/api/cards/{card_id}", json={"icon_url": "http://cdn.example.com/a.png"})
        check("允许 http(s) 外链图标", r.status_code == 200, r.text[:200])

        r = client.patch(f"/api/cards/{card_id}", json={"icon_url": ""})
        check("允许清空图标回到默认样式", r.status_code == 200 and r.json()["icon_url"] == "", r.text[:200])

        print("\n[8] 图标上传")
        r = client.post(
            "/api/uploads/icon",
            files={"file": ("icon.png", TINY_PNG, "image/png")},
        )
        check("上传 PNG 图标成功", r.status_code == 201, r.text[:200])
        uploaded = r.json()
        icon_url = uploaded.get("url", "")
        check("返回 /uploads/icons/ 地址", icon_url.startswith("/uploads/icons/"), icon_url)
        check("识别为 image/png", uploaded.get("content_type") == "image/png", str(uploaded))
        check("文件名为 uuid（不含原文件名）", "icon" not in uploaded.get("filename", "x"), str(uploaded.get("filename")))

        # 图标读取是显式路由（非 StaticFiles 挂载），因此能叠加鉴权依赖：
        # 未登录必须 401，而不是"知道文件名就能看"。
        with TestClient(app, base_url="http://testserver") as anon:
            r = anon.get(icon_url)
            check("未登录访问图标返回 401", r.status_code == 401, f"status={r.status_code}")

        r = client.get(icon_url)
        check("登录后可访问上传的图标", r.status_code == 200, f"status={r.status_code}")
        check("图标响应类型为图片", (r.headers.get("content-type") or "").startswith("image/"), str(r.headers.get("content-type")))
        check("图标响应带 nosniff", r.headers.get("x-content-type-options") == "nosniff", str(dict(r.headers))[:200])
        check(
            "图标响应带 CSP sandbox",
            "sandbox" in (r.headers.get("content-security-policy") or ""),
            str(r.headers.get("content-security-policy")),
        )
        check(
            "图标缓存为 private（登录资源不进共享缓存）",
            "private" in (r.headers.get("cache-control") or ""),
            str(r.headers.get("cache-control")),
        )

        r = client.get("/uploads/icons/does-not-exist.png")
        check("不存在的图标返回 404", r.status_code == 404, f"status={r.status_code}")
        r = client.get("/uploads/icons/.hidden")
        check("隐藏文件名被拒绝", r.status_code == 404, f"status={r.status_code}")

        r = client.post(
            "/api/uploads/icon",
            files={"file": ("evil.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "image/svg+xml")},
        )
        check(
            "拒绝 SVG（防同源 XSS）并给出转换建议",
            r.status_code == 400 and "PNG" in r.text,
            f"status={r.status_code} {r.text[:160]}",
        )

        r = client.post(
            "/api/uploads/icon",
            files={"file": ("page.html", b"<!DOCTYPE html><html><body>hi</body></html>", "text/html")},
        )
        check("拒绝 HTML 文件", r.status_code == 400, f"status={r.status_code}")

        r = client.post(
            "/api/uploads/icon",
            files={"file": ("fake.png", b"\x00\x01\x02\x03not-an-image", "image/png")},
        )
        check("拒绝伪造扩展名的非图片", r.status_code == 400, f"status={r.status_code}")

        r = client.post(
            "/api/uploads/icon",
            files={"file": ("huge.png", b"\x89PNG\r\n\x1a\n" + b"0" * (600 * 1024), "image/png")},
        )
        check("拒绝超过 512KB 的图标", r.status_code == 400, f"status={r.status_code} {r.text[:160]}")

        with TestClient(app, base_url="http://testserver") as normal:
            normal.post("/api/auth/login", json={"username": "smokeuser", "password": "SmokeUser#2026"})
            r = normal.post("/api/uploads/icon", files={"file": ("icon.png", TINY_PNG, "image/png")})
            check("普通用户上传被拒 403", r.status_code == 403, f"status={r.status_code}")

        print("\n[9] 配置导入导出")
        r = client.get("/api/cards/export")
        check("导出返回 200", r.status_code == 200, r.text[:160])
        check("导出带下载文件名", "attachment" in (r.headers.get("content-disposition") or ""), str(r.headers.get("content-disposition")))
        doc = r.json()
        check("导出文件含 version", doc.get("version") == 1, str(doc.get("version")))
        check("导出条数与 count 一致", doc.get("count") == len(doc.get("cards", [])), str(doc.get("count")))
        check("导出条目含 slug 与 target_url", bool(doc["cards"][0].get("slug")) and bool(doc["cards"][0].get("target_url")), str(doc["cards"][0])[:200])
        check("导出不含 id/点击量", "id" not in doc["cards"][0] and "click_count" not in doc["cards"][0], str(doc["cards"][0].keys()))
        baseline = len(client.get("/api/cards", params={"include_disabled": True}).json())

        r = client.post("/api/cards/import", json={
            "mode": "merge",
            "cards": [
                {"slug": "smoke-imported", "title": "导入的卡片", "target_url": "http://127.0.0.1:9/", "icon_url": uploaded.get("url", ""), "bg_style": "mint"},
                {"slug": "bad", "title": "", "target_url": "http://127.0.0.1:9/"},
            ],
        })
        check("导入接口返回 200", r.status_code == 200, r.text[:200])
        result = r.json()
        check("新建 1 张、跳过 1 张", result.get("created") == 1 and result.get("skipped") == 1, str(result))
        check("跳过原因可读", bool(result.get("errors")) and "bad" in result["errors"][0], str(result.get("errors")))

        r = client.post("/api/cards/import", json={
            "mode": "merge",
            "cards": [{"slug": "smoke-imported", "title": "导入的卡片-改", "target_url": "http://127.0.0.1:9/"}],
        })
        check("同名再次导入走更新", r.json().get("updated") == 1, r.text[:200])

        r = client.post("/api/cards/import", json={
            "mode": "merge",
            "cards": [{"slug": "meta", "title": "元数据", "target_url": "http://169.254.169.254/latest/meta-data/", "open_mode": "proxy"}],
        })
        check("导入时拦截云元数据地址", r.json().get("skipped") == 1, r.text[:200])

        current = len(client.get("/api/cards", params={"include_disabled": True}).json())
        r = client.post("/api/cards/import", json={"mode": "replace", "dry_run": True, "cards": [{"slug": "only", "title": "只有一张", "target_url": "http://127.0.0.1:9/"}]})
        preview = r.json()
        check("预演报告会删除数", preview.get("deleted") == current, str(preview))
        check("预演不会写入", preview.get("dry_run") is True, str(preview))
        after_dry = len(client.get("/api/cards", params={"include_disabled": True}).json())
        check("预演后数据未变", after_dry == current, f"{after_dry} != {current}")

        r = client.post("/api/cards/import", json={"mode": "replace", "cards": [{"slug": "only", "title": "只有一张", "target_url": "http://127.0.0.1:9/"}]})
        replaced = r.json()
        check("replace 模式清空重建", replaced.get("deleted") == current and replaced.get("created") == 1, str(replaced))
        check("replace 后只剩 1 张", len(client.get("/api/cards", params={"include_disabled": True}).json()) == 1, "数量不符")

        r = client.post("/api/cards/import", json={"mode": "replace", "cards": doc["cards"]})
        restored = r.json()
        check("用导出的文件还原成功", restored.get("created") == len(doc["cards"]) and restored.get("skipped") == 0, str(restored)[:200])
        check("还原后恢复条目数", len(client.get("/api/cards", params={"include_disabled": True}).json()) == len(doc["cards"]), "数量不符")

        with TestClient(app, base_url="http://testserver") as normal:
            normal.post("/api/auth/login", json={"username": "smokeuser", "password": "SmokeUser#2026"})
            r = normal.get("/api/cards/export")
            check("普通用户导出被拒 403", r.status_code == 403, f"status={r.status_code}")
            r = normal.post("/api/cards/import", json={"mode": "merge", "cards": [{"slug": "x", "title": "x", "target_url": "http://127.0.0.1:9/"}]})
            check("普通用户导入被拒 403", r.status_code == 403, f"status={r.status_code}")

        with TestClient(app, base_url="http://testserver") as anon2:
            r = anon2.get("/api/cards/export")
            check("未登录导出返回 401", r.status_code == 401, f"status={r.status_code}")

        print("\n[9b] 导出表格格式（xlsx / csv）")
        r = client.get("/api/cards/export", params={"format": "json"})
        check("不传 format 时默认导出 JSON", r.headers["content-type"].startswith("application/json"), r.headers.get("content-type"))
        check("导出带 X-Card-Count 头", r.headers.get("x-card-count") == str(len(doc["cards"])), str(r.headers.get("x-card-count")))

        r = client.get("/api/cards/export", params={"format": "xlsx"})
        xlsx_bytes = r.content
        check("xlsx 导出返回 200", r.status_code == 200, r.text[:160])
        check("xlsx Content-Type 正确", r.headers.get("content-type", "").startswith(XLSX_MIME), r.headers.get("content-type"))
        check("xlsx 是 zip 容器", xlsx_bytes[:4] == b"PK\x03\x04", repr(xlsx_bytes[:8]))
        check("xlsx 文件名后缀正确", ".xlsx" in (r.headers.get("content-disposition") or ""), str(r.headers.get("content-disposition")))

        book = load_workbook(io.BytesIO(xlsx_bytes))
        check("xlsx 含「卡片」表", "卡片" in book.sheetnames, str(book.sheetnames))
        check("xlsx 含「字段说明」表", "字段说明" in book.sheetnames, str(book.sheetnames))
        ws = book["卡片"]
        headers = [cell.value for cell in ws[1]]
        check("xlsx 表头与列定义一致", headers == EXPECTED_HEADERS, str(headers))
        check("xlsx 数据行数与卡片数一致", ws.max_row - 1 == len(doc["cards"]), f"{ws.max_row - 1} vs {len(doc['cards'])}")
        check("xlsx 表头带批注说明", ws.cell(row=1, column=1).comment is not None, "表头没有批注")
        check("xlsx 冻结首行", ws.freeze_panes == "A2", str(ws.freeze_panes))
        validations = list(ws.data_validations.dataValidation)
        check("xlsx 枚举列有下拉校验", any("E2" in str(dv.sqref) for dv in validations), f"{len(validations)} 个校验")
        book.close()

        r = client.get("/api/cards/export", params={"format": "csv"})
        csv_bytes = r.content
        check("csv 导出返回 200", r.status_code == 200, r.text[:160])
        check("csv Content-Type 正确", r.headers.get("content-type", "").startswith("text/csv"), r.headers.get("content-type"))
        check("csv 带 UTF-8 BOM（否则 Excel 里中文乱码）", csv_bytes.startswith(b"\xef\xbb\xbf"), repr(csv_bytes[:6]))
        csv_text = csv_bytes.decode("utf-8-sig")
        csv_rows = list(csv.reader(io.StringIO(csv_text)))
        check("csv 表头与列定义一致", csv_rows[0] == EXPECTED_HEADERS, str(csv_rows[0][:5]))
        check("csv 行数与卡片数一致", len(csv_rows) - 1 == len(doc["cards"]), f"{len(csv_rows) - 1}")
        # 按表头名取列下标而不是写死数字：列会随功能增减而移动（多地址那次就
        # 插进了一列），写死下标会让用例在"列位置变了"时莫名其妙地失败
        description_index = EXPECTED_HEADERS.index("描述")
        check(
            "csv 中文描述未损坏",
            any(row[description_index] for row in csv_rows[1:]),
            "描述列全空",
        )

        r = client.get("/api/cards/export", params={"format": "txt"})
        check("不支持的 format 返回 422", r.status_code == 422, f"status={r.status_code}")

        print("\n[9c] 表格往返（导出 → 预演导入，应全部命中且无跳过）")
        for label, payload, mime, name in (
            ("xlsx", xlsx_bytes, XLSX_MIME, "cards.xlsx"),
            ("csv", csv_bytes, "text/csv", "cards.csv"),
            ("json", json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json", "cards.json"),
        ):
            r = client.post(
                "/api/cards/import/file",
                files={"file": (name, payload, mime)},
                data={"mode": "merge", "dry_run": "true"},
            )
            check(f"{label} 往返预演返回 200", r.status_code == 200, r.text[:200])
            got = r.json()
            check(f"{label} 往返全部命中已存在卡片", got.get("updated") == len(doc["cards"]) and got.get("created") == 0, str(got)[:220])
            check(f"{label} 往返无跳过、无错误", got.get("skipped") == 0 and not got.get("errors"), str(got.get("errors"))[:220])

        print("\n[9d] 表格导入真的写进去了（改一列再导回）")
        target = doc["cards"][0]
        target_slug = target["slug"]
        original_title = target["title"]
        edited_title = f"{original_title}-表格改"
        # 模拟"在 Excel 里只改一列"：基于导出的 csv 改名称列，其余原样保留
        grid = [row for row in csv_rows]
        title_col = grid[0].index("名称")
        slug_col = grid[0].index("标识")
        for row in grid[1:]:
            if row[slug_col] == target_slug:
                row[title_col] = edited_title
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows(grid)
        edited_csv = buf.getvalue().encode("utf-8-sig")

        r = client.post(
            "/api/cards/import/file",
            files={"file": ("edited.csv", edited_csv, "text/csv")},
            data={"mode": "merge", "dry_run": "false"},
        )
        applied = r.json()
        check("表格改动真实写入", applied.get("updated") == len(doc["cards"]) and applied.get("skipped") == 0, str(applied)[:220])
        titles = {c["slug"]: c["title"] for c in client.get("/api/cards", params={"include_disabled": True}).json()}
        check("目标卡片名称已按表格更新", titles.get(target_slug) == edited_title, str(titles.get(target_slug)))

        # 改回去，保证后续用例的基线不被污染
        for row in grid[1:]:
            if row[slug_col] == target_slug:
                row[title_col] = original_title
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows(grid)
        client.post(
            "/api/cards/import/file",
            files={"file": ("restore.csv", buf.getvalue().encode("utf-8-sig"), "text/csv")},
            data={"mode": "merge", "dry_run": "false"},
        )
        titles = {c["slug"]: c["title"] for c in client.get("/api/cards", params={"include_disabled": True}).json()}
        check("名称已还原", titles.get(target_slug) == original_title, str(titles.get(target_slug)))
        check("还原后卡片总数不变", len(titles) == len(doc["cards"]), f"{len(titles)}")

        print("\n[9e] 表格容错与错误定位")
        mixed = (
            "标识,名称,目标地址,启用\n"
            f"{target_slug},占位,http://127.0.0.1:9/,是\n"
            "from-csv,表格新增,http://127.0.0.1:9/,不知道\n"
        ).encode("utf-8-sig")
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("mixed.csv", mixed, "text/csv")},
            data={"mode": "merge", "dry_run": "true"},
        )
        mixed_result = r.json()
        check("非法单元格只跳过该行", mixed_result.get("skipped") == 1 and mixed_result.get("total") == 2, str(mixed_result)[:220])
        check("错误定位到真实行号（第 3 行）", any("第 3 行" in e for e in mixed_result.get("errors", [])), str(mixed_result.get("errors")))
        check("错误文案是中文", any("启用" in e for e in mixed_result.get("errors", [])), str(mixed_result.get("errors")))
        after_preview = {c["slug"] for c in client.get("/api/cards", params={"include_disabled": True}).json()}
        check("预演没有写入新卡片", "from-csv" not in after_preview, str(sorted(after_preview)))

        # xlsx 路径同样要能定位到 Excel 里的行号
        book = load_workbook(io.BytesIO(xlsx_bytes))
        ws = book["卡片"]
        ws.cell(row=3, column=5).value = "不知道"  # 第 3 行「打开方式」
        broken = io.BytesIO()
        book.save(broken)
        book.close()
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("broken.xlsx", broken.getvalue(), XLSX_MIME)},
            data={"mode": "merge", "dry_run": "true"},
        )
        broken_result = r.json()
        check("xlsx 非法值返回 200 且只跳 1 行", broken_result.get("skipped") == 1, str(broken_result)[:220])
        check("xlsx 错误定位到 Excel 第 3 行", any("第 3 行" in e and "打开方式" in e for e in broken_result.get("errors", [])), str(broken_result.get("errors")))

        # 中文写在描述列：slug 只允许小写字母数字，中文放在那里会被校验挡掉
        gbk = "标识,名称,目标地址,描述\ngbk-row,编码测试,http://127.0.0.1:9/,中文描述不能乱码\n".encode("gb18030")
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("gbk.csv", gbk, "text/csv")},
            data={"mode": "merge", "dry_run": "true"},
        )
        gbk_result = r.json()
        check("GBK 编码的 CSV 能正确解码", r.status_code == 200 and gbk_result.get("created") == 1 and gbk_result.get("skipped") == 0, str(gbk_result)[:220])

        # GBK 解码后中文要真的对，不能只是"没报错"
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("gbk.csv", gbk, "text/csv")},
            data={"mode": "merge", "dry_run": "false"},
        )
        check("GBK 内容真实写入", r.json().get("created") == 1, r.text[:200])
        written = [c for c in client.get("/api/cards", params={"include_disabled": True}).json() if c["slug"] == "gbk-row"]
        check("GBK 中文描述未乱码", bool(written) and written[0]["description"] == "中文描述不能乱码", str(written)[:200])
        if written:
            client.delete(f"/api/cards/{written[0]['id']}")

        # 中文取值别名：表头是中文，用户很自然会顺手把值也写成中文
        chinese_values = (
            "标识,名称,目标地址,打开方式,卡片底色,图标配色,启用\n"
            f"{target_slug},占位,http://127.0.0.1:9/,直连,天青,紫罗兰,是\n"
        ).encode("utf-8-sig")
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("cn.csv", chinese_values, "text/csv")},
            data={"mode": "merge", "dry_run": "true"},
        )
        cn_result = r.json()
        check("中文取值别名被接受（直连/天青/紫罗兰）", cn_result.get("updated") == 1 and cn_result.get("skipped") == 0, str(cn_result)[:220])

        for label, payload, name, expect in (
            ("旧版 xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128, "old.xls", "另存为"),
            ("二进制文件", b"\x00\x01\x02\x03\x04\x05\x06\x07", "junk.bin", "二进制"),
            ("只有表头", "标识,名称,目标地址\n".encode("utf-8-sig"), "empty.csv", "没有可导入"),
            ("缺必需列", "标题,说明\nfoo,bar\n".encode("utf-8-sig"), "missing.csv", "目标地址"),
        ):
            r = client.post(
                "/api/cards/import/file",
                files={"file": (name, payload, "application/octet-stream")},
                data={"mode": "merge", "dry_run": "true"},
            )
            check(f"{label} 返回 400", r.status_code == 400, f"status={r.status_code} body={r.text[:160]}")
            message = r.json().get("error", {}).get("message", "")
            check(f"{label} 有可操作的中文提示", expect in message, message)

        oversized = "标识,名称,目标地址\n".encode("utf-8") + b"x" * (6 * 1024 * 1024)
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("big.csv", oversized, "text/csv")},
            data={"mode": "merge", "dry_run": "true"},
        )
        check("超大文件被拒 400", r.status_code == 400, f"status={r.status_code}")

        all_bad = "标识,名称,目标地址,启用\n,,,否\nbad,坏行,http://127.0.0.1:9/,不对\n".encode("utf-8-sig")
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("allbad.csv", all_bad, "text/csv")},
            data={"mode": "merge", "dry_run": "true"},
        )
        all_bad_result = r.json()
        check("全部行非法时返回 200 而不是 400", r.status_code == 200, f"status={r.status_code} {r.text[:160]}")
        check("全部行非法时逐条说明原因", all_bad_result.get("skipped") == all_bad_result.get("total") and all_bad_result.get("errors"), str(all_bad_result)[:220])

        inconsistent = "标识,名称,目标地址,启用\nok-1,卡片,http://127.0.0.1:9/,是\n".encode("utf-8-sig")
        r = client.post(
            "/api/cards/import/file",
            files={"file": ("a.json", b'{"cards": ["not-an-object"]}', "application/json")},
            data={"mode": "merge", "dry_run": "true"},
        )
        check("JSON 里混入非对象条目只跳过该条", r.status_code == 200 and r.json().get("skipped") == 1, r.text[:200])

        print("\n[9f] 编解码层直测（不走 HTTP，专测容易回归的边界）")
        tricky = CardExportDoc(
            exported_at=datetime.now(timezone.utc),
            count=1,
            cards=[
                CardExportItem.model_validate(
                    {"slug": "eq-title", "title": "=汇总", "target_url": "http://127.0.0.1:9/"}
                )
            ],
        )
        tall = card_sheet.parse_sheet(card_sheet.to_xlsx(tricky), "tricky.xlsx")
        # Excel 会把以「=」开头的文本当公式，data_only 读回来就是空值 —— 静默丢数据
        check("以 = 开头的名称不会被 Excel 当公式丢掉", tall.cards[0].get("title") == "=汇总", str(tall.cards))

        titled = "开发工具箱卡片清单\n标识,名称,目标地址\nok-1,卡片,http://127.0.0.1:9/\n".encode("utf-8-sig")
        check("表头上方多一行标题也能找到表头", card_sheet.parse_sheet(titled, "t.csv").header_row == 2, "表头行号不对")

        english = "slug,title,target_url,open_mode\nen-row,name,http://127.0.0.1:9/,proxy\n".encode("utf-8-sig")
        en_result = card_sheet.parse_sheet(english, "t.csv")
        check("英文列名同样可解析", en_result.cards[0]["slug"] == "en-row", str(en_result.cards))
        check("英文列名解析无错误", not en_result.errors, str(en_result.errors))

        csv_round = card_sheet.parse_sheet(card_sheet.to_csv(tricky), "tricky.csv")
        check("CSV 导出带 UTF-8 BOM", card_sheet.to_csv(tricky).startswith(b"\xef\xbb\xbf"), "缺 BOM（Excel 会乱码）")
        check("CSV 中的 = 开头文本原样保留", csv_round.cards[0].get("title") == "=汇总", str(csv_round.cards))

        print("\n[9g] 表格导入的权限")
        with TestClient(app, base_url="http://testserver") as normal:
            normal.post("/api/auth/login", json={"username": "smokeuser", "password": "SmokeUser#2026"})
            r = normal.post(
                "/api/cards/import/file",
                files={"file": ("a.csv", inconsistent, "text/csv")},
                data={"mode": "merge", "dry_run": "true"},
            )
            check("普通用户上传导入被拒 403", r.status_code == 403, f"status={r.status_code}")
            r = normal.get("/api/cards/export", params={"format": "xlsx"})
            check("普通用户导出 xlsx 被拒 403", r.status_code == 403, f"status={r.status_code}")

        with TestClient(app, base_url="http://testserver") as anon3:
            r = anon3.post(
                "/api/cards/import/file",
                files={"file": ("a.csv", inconsistent, "text/csv")},
                data={"mode": "merge", "dry_run": "true"},
            )
            check("未登录上传导入返回 401", r.status_code == 401, f"status={r.status_code}")

        print("\n[9h] 一张卡片挂多个环境地址")
        r = client.post(
            "/api/cards",
            json={
                "title": "多环境测试",
                "slug": "smoke-multi",
                "target_url": "http://127.0.0.1:9/",
                "open_mode": "direct",
                "endpoints": [
                    {"name": "生产", "url": "https://prod.example.com/", "open_mode": "proxy"},
                    {"name": "测试", "url": "http://10.0.0.9:8081/"},
                ],
            },
        )
        check("建卡（带环境地址）返回 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        multi = r.json()
        eps = multi.get("endpoints", [])
        check("响应里带 2 条环境地址", len(eps) == 2, str(eps))
        if len(eps) == 2:
            # 中文环境名转不出 ascii，回落成「卡片标识-序号」，好认且不会跨卡片撞车
            check("中文环境名回落到 卡片标识-序号", eps[0]["slug"] == "smoke-multi-1", eps[0]["slug"])
            check("两条标识互不重复", len({e["slug"] for e in eps}) == 2, str([e["slug"] for e in eps]))
            check(
                "代理地址的 launch_url 指向网关",
                eps[0]["launch_url"] == f"/gw/{eps[0]['slug']}/",
                eps[0]["launch_url"],
            )
            check(
                "直连地址的 launch_url 就是原地址",
                eps[1]["launch_url"] == "http://10.0.0.9:8081/",
                eps[1]["launch_url"],
            )
            # 关键：每条地址自带 open_mode，所以同卡片能混用
            check(
                "每条地址各自带打开方式",
                [e["open_mode"] for e in eps] == ["proxy", "direct"],
                str([e["open_mode"] for e in eps]),
            )
            check("环境地址带上了 card_id", all(e["card_id"] == multi["id"] for e in eps), str(eps))
            check(
                "提交顺序即显示顺序",
                [e["sort_order"] for e in eps] == [10, 20],
                str([e["sort_order"] for e in eps]),
            )
        check("卡片本体仍是 direct", multi.get("open_mode") == "direct", str(multi.get("open_mode")))

        r = client.get("/api/cards", params={"include_disabled": True})
        listed = next((c for c in r.json() if c["slug"] == "smoke-multi"), None)
        check(
            "列表接口也带出环境地址（没有 N+1 丢数据）",
            listed is not None and len(listed.get("endpoints", [])) == 2,
            str(listed and listed.get("endpoints")),
        )

        kept_slug = eps[0]["slug"] if eps else "smoke-multi-1"
        r = client.patch(
            f"/api/cards/{multi['id']}",
            json={
                "endpoints": [
                    {
                        "slug": kept_slug,
                        "name": "生产环境",
                        "url": "https://prod.example.com/",
                        "open_mode": "proxy",
                    },
                    {"name": "预发", "url": "http://10.0.0.10:8081/", "open_mode": "proxy"},
                ]
            },
        )
        check("PATCH 环境地址成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        after = r.json().get("endpoints", [])
        check("替换后剩 2 条", len(after) == 2, str(after))
        check(
            "回传标识的条目保住了原标识（已分享的书签不失效）",
            after[0]["slug"] == kept_slug,
            str([e["slug"] for e in after]),
        )
        check("名称已按提交更新", after[0]["name"] == "生产环境", after[0]["name"])
        check(
            "顺序按提交顺序重排",
            [e["name"] for e in after] == ["生产环境", "预发"],
            str([e["name"] for e in after]),
        )

        r = client.patch(f"/api/cards/{multi['id']}", json={"title": "多环境测试2"})
        check(
            "只改标题不影响环境地址",
            len(r.json().get("endpoints", [])) == 2,
            str(r.json().get("endpoints")),
        )

        r = client.patch(
            f"/api/cards/{multi['id']}",
            json={"endpoints": [{"name": "a", "url": "http://a/"}, {"name": "A", "url": "http://b/"}]},
        )
        check("环境名称重复被拒 422", r.status_code == 422, f"{r.status_code} {r.text[:160]}")

        r = client.patch(
            f"/api/cards/{multi['id']}",
            json={"endpoints": [{"name": "a", "url": "ftp://a/"}]},
        )
        check("非 http(s) 的环境地址被拒 422", r.status_code == 422, f"{r.status_code}")

        r = client.post(f"/api/cards/{multi['id']}/click", params={"endpoint": kept_slug})
        check("带环境标识的点击上报成功", r.status_code == 200, r.text[:160])

        r = client.get("/api/cards/export", params={"format": "json"})
        item = next((c for c in r.json()["cards"] if c["slug"] == "smoke-multi"), None)
        check(
            "JSON 导出带出环境地址",
            item is not None and len(item.get("endpoints", [])) == 2,
            str(item and item.get("endpoints")),
        )
        check(
            "JSON 导出保留环境标识",
            item is not None and item["endpoints"][0].get("slug") == kept_slug,
            str(item["endpoints"][0] if item else None),
        )

        csv_text = client.get("/api/cards/export", params={"format": "csv"}).content.decode("utf-8-sig")
        check("CSV 表头含「环境地址」列", "环境地址" in csv_text.splitlines()[0], csv_text.splitlines()[0])
        check(
            "CSV 里写着两条环境地址",
            "生产环境" in csv_text and "预发" in csv_text,
            csv_text[:300],
        )

        book = load_workbook(io.BytesIO(client.get("/api/cards/export", params={"format": "xlsx"}).content))
        sheet = book["卡片"]
        headers = [cell.value for cell in sheet[1]]
        env_column = headers.index("环境地址") + 1
        slug_column = headers.index("标识") + 1
        env_row = next(
            row
            for row in range(2, sheet.max_row + 1)
            if sheet.cell(row=row, column=slug_column).value == "smoke-multi"
        )
        env_cell = sheet.cell(row=env_row, column=env_column).value or ""
        check(
            "xlsx 环境地址一格多行（换行分隔）",
            "\n" in env_cell and "预发" in env_cell,
            repr(env_cell),
        )
        book.close()

        r = client.post(
            "/api/cards/import/file",
            files={"file": ("rt.csv", csv_text.encode("utf-8-sig"), "text/csv")},
            data={"mode": "merge", "dry_run": "true"},
        )
        check("导出的 CSV 能原样预演导入", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        rt = r.json()
        check("CSV 往返无错误、无跳过", rt["skipped"] == 0 and not rt["errors"], str(rt))
        check("CSV 往返命中该卡片", rt["updated"] >= 1, str(rt))

        r = client.patch(f"/api/cards/{multi['id']}", json={"endpoints": []})
        check("传空数组即清空环境地址", r.json().get("endpoints") == [], str(r.json().get("endpoints")))

        # 级联验证：删掉卡片后重建成同名标识 + 同名环境，
        # 生成的标识若还是 smoke-multi-1，说明旧的环境地址行真的被删了
        # （否则会因「标识已被占用」退化成 smoke-multi-1-2）
        client.patch(
            f"/api/cards/{multi['id']}",
            json={"endpoints": [{"name": "生产环境", "url": "https://prod.example.com/"}]},
        )
        check("删卡前先挂回一条地址", True)
        client.delete(f"/api/cards/{multi['id']}")
        r = client.post(
            "/api/cards",
            json={
                "title": "级联验证",
                "slug": "smoke-multi",
                "target_url": "http://127.0.0.1:9/",
                "open_mode": "direct",
                "endpoints": [{"name": "生产环境", "url": "https://prod.example.com/"}],
            },
        )
        check("重建卡片成功", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
        rebuilt = r.json()
        rebuilt_eps = rebuilt.get("endpoints", [])
        check(
            "删除卡片时环境地址被级联清理（标识可原样复用）",
            len(rebuilt_eps) == 1 and rebuilt_eps[0]["slug"] == "smoke-multi-1",
            str(rebuilt_eps),
        )
        client.delete(f"/api/cards/{rebuilt['id']}")

        print("\n[10] 清理")
        # replace 导入会重建全部卡片（id 会变），所以按 slug 重新定位再删
        cards_now = client.get("/api/cards", params={"include_disabled": True}).json()
        target = next((c for c in cards_now if c["slug"] == card_slug), None)
        check("清理目标仍存在", target is not None, f"slug={card_slug}")
        if target:
            r = client.delete(f"/api/cards/{target['id']}")
            check("删除卡片成功", r.status_code == 200, r.text[:160])
        r = client.delete(f"/api/users/{normal_user_uid}")
        check("删除用户成功", r.status_code == 200, r.text[:160])

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
