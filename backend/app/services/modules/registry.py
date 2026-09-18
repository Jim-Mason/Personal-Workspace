"""模块注册表：发现模块、持有它们的运行期状态。

注册表只回答两类问题：
1. 「有哪些模块、谁负责哪段路径」——给路由与导航用
2. 「某个模块现在是什么状态」——给日志、健康检查、前端状态灯用

进程怎么起、怎么收，归 supervisor 管；这里只存状态，不发起动作。
这样 supervisor 可以单独测试，而注册表在没有子进程时也能正常工作
（`native` / `static` 模块根本不需要子进程）。
"""

from __future__ import annotations

import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .spec import ModuleSpec, ModuleSpecError, load_spec

# 状态机：stopped → starting → running → stopping → stopped
#                    ↘ failed（起不来，error 里有原因）
STATES = ("stopped", "starting", "running", "stopping", "failed")

_LOG_LINES = 400


@dataclass
class ModuleRuntime:
    """某个模块这一次运行期的全部易变状态。中台重启即全部作废。"""

    spec: ModuleSpec
    state: str = "stopped"
    error: str = ""
    pid: int | None = None
    started_at: str = ""
    finished_at: str = ""
    # 上次启动时发现端口已被占用但健康检查通过 —— 当作「上次残留的进程」接管，
    # 不重复拉起。前端要能一眼看出这次不是中台起的。
    attached: bool = False
    health_checked_at: str = ""
    log: deque = field(default_factory=lambda: deque(maxlen=_LOG_LINES))
    reloads: int = 0
    # 声明改过、但进程还是按旧声明起的 —— 前端要提示「重启后生效」
    spec_stale: bool = False
    # 一次性提示是否已经说过（比如「已剥离外部 CDN 资源」）。
    # 这类消息每个 HTML 响应都会触发一次，不记号的话日志会被它刷满，
    # 真正有用的那几行反而被淹掉。
    announced: set = field(default_factory=set)

    def announce_once(self, key: str, message: str) -> bool:
        if key in self.announced:
            return False
        self.announced.add(key)
        self.log.append(message)
        return True

    # 模块级票据：浏览器里模块页面要拿它做子资源鉴权。
    #
    # 为什么不直接把中台令牌给它：模块是第三方代码，中台令牌一旦落进它的
    # 页面 JavaScript，它就能反过来调中台的 /api/*。票据只对「本模块的那段
    # mount 前缀」有效，偷走也够不到中台。
    ticket: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def cookie_name(self) -> str:
        return f"pw_mod_{self.spec.id}"

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attached": self.attached,
            "health_checked_at": self.health_checked_at,
            "spec_stale": self.spec_stale,
            "log_tail": list(self.log)[-_LOG_LINES:],
        }


class ModuleRegistry:
    def __init__(self, modules_dir: Path) -> None:
        self._dir = modules_dir
        self._lock = threading.RLock()
        self._runtime: dict[str, ModuleRuntime] = {}
        self._errors: dict[str, str] = {}
        self.reload()

    # ------------------------------------------------------------ 发现
    def reload(self) -> dict:
        """重新扫描 modules/ 目录。

        注意：**不打断正在运行的模块**。已经跑着的模块用原 spec 继续跑
        （进程是按旧 spec 起的，忽然换一套会让状态对不上），
        但声明本身会刷新，下次重启生效。这是刻意的取舍 —— 改完声明后
        你会看到状态灯变成「需重启」，而不是服务莫名其妙换了个端口。
        """
        found: list[ModuleSpec] = []
        errors: dict[str, str] = {}
        if self._dir.is_dir():
            for child in sorted(self._dir.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                if not (child / "module.py").exists():
                    # 没声明的目录不算错：可能是模块自己的源码目录、或是还没接的草稿
                    continue
                try:
                    found.append(load_spec(child))
                except ModuleSpecError as exc:
                    errors[child.name] = str(exc)

        with self._lock:
            for spec in found:
                runtime = self._runtime.get(spec.id)
                if runtime is None:
                    self._runtime[spec.id] = ModuleRuntime(spec=spec)
                else:
                    # 保留进程/日志/票据，只换声明，并标一次「声明已变」
                    if runtime.spec != spec:
                        runtime.reloads += 1
                        runtime.spec_stale = runtime.state != "stopped"
                        runtime.log.append(
                            "[中台] 检测到 module.py 变化；新声明将在下次重启该模块时生效"
                        )
                    runtime.spec = spec
            for module_id in list(self._runtime):
                if module_id not in {s.id for s in found}:
                    # 目录被删了：进程还在就留着，避免把手上的进程搞成孤儿
                    if self._runtime[module_id].state == "running":
                        continue
                    del self._runtime[module_id]
            self._errors = errors

        return {
            "loaded": sorted(s.id for s in found),
            "errors": errors,
        }

    # ------------------------------------------------------------ 查询
    def all(self) -> list[ModuleSpec]:
        with self._lock:
            specs = [r.spec for r in self._runtime.values()]
        return sorted(specs, key=lambda s: (s.order, s.id))

    def runtime(self, module_id: str) -> ModuleRuntime | None:
        with self._lock:
            return self._runtime.get(module_id)

    def runtimes(self) -> list[ModuleRuntime]:
        with self._lock:
            return sorted(self._runtime.values(), key=lambda r: (r.spec.order, r.spec.id))

    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)

    def by_mount(self, path: str) -> tuple[ModuleSpec, str] | None:
        """按最长前缀匹配，返回 (模块, 去掉前缀后的剩余路径)。

        `/opsgen/api/x` → (opsgen, `api/x`)；`/opsgen` 也能命中，剩余为空。
        必须按段边界比对，否则 `/opsgenx` 会被误判成 `/opsgen` 下的路径。
        """
        candidate = path.split("?", 1)[0]
        best: tuple[ModuleSpec, str] | None = None
        for spec in self.all():
            if not spec.enabled:
                continue
            mount = spec.mount
            if candidate == mount:
                rest = ""
            elif candidate.startswith(mount + "/"):
                rest = candidate[len(mount) + 1 :]
            else:
                continue
            if best is None or len(mount) > len(best[0].mount):
                best = (spec, rest)
        return best

    def ticket_map(self) -> dict[str, str]:
        """mount → 票据。给 security 做鉴权判定用。"""
        with self._lock:
            return {r.spec.mount: r.ticket for r in self._runtime.values()}

    def ticket_for(self, module_id: str) -> str:
        runtime = self.runtime(module_id)
        return runtime.ticket if runtime else ""
