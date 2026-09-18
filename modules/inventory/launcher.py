"""启动资源。

**边界说明（重要）**：本版本只允许两种动作 ——
用系统默认程序打开一个已存在的路径，或用默认浏览器打开一个网址。
刻意不提供「执行任意命令行」的能力，因为那等于把整个中台变成一个
任何人都能通过浏览器调用的命令执行器。脚本执行会作为独立能力加入，
届时必须配套白名单 + 逐次确认 + 参数模板，不能顺手开个口子。
"""

from __future__ import annotations

import os
import sqlite3
import time
import webbrowser
from pathlib import Path

from . import store


class LaunchError(Exception):
    """可以展示给用户看的失败原因。"""


def _open_with_system(path_text: str) -> None:
    target = Path(path_text)
    if not target.exists():
        raise LaunchError(f"路径不存在或已被移动：{path_text}")
    if os.name != "nt":
        raise LaunchError("当前版本只支持 Windows")
    # Windows 专用：交给系统按扩展名决定用什么程序打开。
    os.startfile(str(target))  # type: ignore[attr-defined]


def launch(conn: sqlite3.Connection, resource: dict, *, machine_id: str, ip: str = "") -> dict:
    """启动一条资源，并留下 task 与 event 两条流水。"""
    res_type = resource.get("type")
    target = resource.get("path_or_url") or ""
    started = time.perf_counter()
    status = "ok"
    message: str | None = None

    try:
        if res_type == "url":
            if not target.lower().startswith(("http://", "https://")):
                raise LaunchError("网址必须以 http:// 或 https:// 开头")
            webbrowser.open(target)
        elif res_type in {"app", "file", "folder"}:
            _open_with_system(target)
        elif res_type == "script":
            raise LaunchError("脚本执行能力尚未开放，将在配备白名单与确认机制后提供")
        else:
            raise LaunchError(f"不支持的类型：{res_type}")
    except LaunchError as exc:
        status = "failed"
        message = str(exc)
    except OSError as exc:
        status = "failed"
        message = f"系统调用失败：{exc}"

    duration_ms = int((time.perf_counter() - started) * 1000)

    task_id = store.record_task(
        conn,
        kind="launch",
        machine_id=machine_id,
        status=status,
        resource_id=resource.get("id"),
        item_name=resource.get("name"),
        exit_code=0 if status == "ok" else 1,
        duration_ms=duration_ms,
        message=message,
    )
    store.record_event(
        conn,
        action="app.launch" if status == "ok" else "app.launch_failed",
        target_type=resource.get("type"),
        target_id=resource.get("id"),
        target_name=resource.get("name"),
        detail={"target": target, "status": status, "message": message},
        ip=ip,
    )
    if status == "ok":
        store.mark_used(conn, int(resource["id"]))

    return {"status": status, "message": message, "task_id": task_id, "duration_ms": duration_ms}
