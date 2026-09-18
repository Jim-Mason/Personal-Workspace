"""模块子进程的生命周期。

这个文件承担三件别人容易做错的事，都在注释里写清楚了原因：

1. **监听地址不由模块决定。** OpsGen 上游写的是 `host="0.0.0.0", debug=True`。
   按约定不动上游源码，所以中台用自己的启动壳（runners/）去 import 它的
   app 对象再自己起服务 —— 地址恒为 127.0.0.1，调试模式恒为关。
   顺带一个好处：`debug=True` 的 Werkzeug 调试器本身就是一个 RCE 入口，
   而它恰恰是最容易被忽略的那类「开发方便、上线致命」的默认值。

2. **端口先探测再用。** 端口被占用时如果不报错，模块会静默起不来，
   前端拿到的是一堆 502/404，排查会从「代理」一路查到「模块」浪费时间。
   所以宁可在启动前就明确失败，并把「谁占着」写进错误信息。

3. **回收必须用进程树。** Windows 上 uvicorn / werkzeug 的 reloader 是
   父 + 子两个同名进程，只 kill 父进程会留下孤儿子进程继续占着端口，
   下一次启动就变成「端口被占用」。所以统一走 `taskkill /T`。

另外中台自己对每个模块进程只做「拉起 / 探活 / 收走」，不解析它的输出语义。
日志原样落到 data/module-logs/<id>.log，方便事后翻。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ... import config
from .registry import ModuleRegistry, ModuleRuntime

_IS_WINDOWS = os.name == "nt"
_PROBE_TIMEOUT = 1.5


class ModuleStartError(Exception):
    """可以直接展示给用户的启动失败原因。"""


# ---------------------------------------------------------------- 工具


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _port_holder(port: int) -> str:
    """端口被占了，尽量说清是谁占的。

    只说「端口被占用」等于把排查丢回给用户；带上 PID 才能一眼定位。
    """
    if not _IS_WINDOWS:
        return ""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            pid = parts[4]
            name = ""
            try:
                name = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    encoding="utf-8",
                    errors="replace",
                ).stdout.strip().split(",")[0].strip('"')
            except (OSError, subprocess.SubprocessError):
                pass
            return f"（PID {pid}{' / ' + name if name else ''}）"
    return ""


def resolve_python(spec_module_dir: Path, explicit: str) -> Path:
    """模块用哪个解释器。

    优先：声明里写死的 > 模块私有 venv > 中台自己的解释器。
    选错解释器的典型症状是 ImportError: No module named flask，
    所以启动日志里会把实际用的是哪个打出来，省一轮排查。
    """
    if explicit:
        return Path(explicit)
    for candidate in (
        spec_module_dir / ".venv" / "Scripts" / "python.exe",
        spec_module_dir / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _health_ok(port: int, path: str) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310 - 固定回环地址
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 说明服务已经在监听，只是这个路径没有内容。这就算"起来了"。
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 连不上就是没起来
        return False, f"{type(exc).__name__}"


def _terminate_tree(pid: int) -> None:
    if pid <= 0:
        return
    if _IS_WINDOWS:
        # /T 连子进程一起收，缺了它就会留下占着端口的孤儿
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return
    try:
        import signal

        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ---------------------------------------------------------------- 主体


class ModuleSupervisor:
    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry
        self._lock = threading.RLock()
        config.MODULE_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------- 启动
    def start(self, module_id: str, *, timeout: float | None = None) -> dict:
        runtime = self._registry.runtime(module_id)
        if runtime is None:
            raise ModuleStartError(f"没有这个模块：{module_id}")
        spec = runtime.spec
        if not spec.enabled:
            raise ModuleStartError(f"模块 {module_id} 已被禁用（module.py 里 enabled=False）")
        if spec.kind not in {"subprocess_proxy", "external"}:
            raise ModuleStartError(f"模块 {module_id} 的接入方式是 {spec.kind}，不需要单独启进程")

        with self._lock:
            if runtime.state in {"running", "starting"}:
                return {"state": runtime.state, "message": "已在运行，无需重复启动"}
            return self._spawn(runtime, timeout=timeout)

    def _spawn(self, runtime: ModuleRuntime, *, timeout: float | None) -> dict:
        spec = runtime.spec
        module_dir = self._registry_dir() / spec.id
        if not module_dir.is_dir():
            raise ModuleStartError(f"模块目录不存在：{module_dir}")

        wait = float(timeout if timeout is not None else spec.startup_timeout)
        runtime.state = "starting"
        runtime.error = ""
        runtime.log.clear()

        # ---- external：模块自己启动，中台只探活 + 反代
        if spec.kind == "external":
            return self._attach_or_fail(
                runtime, why="external 模块由外部自行启动"
            )

        runner = config.RUNNERS_DIR / f"{spec.runner}.py"
        if not runner.exists():
            raise ModuleStartError(
                f"中台没有名为 {spec.runner!r} 的启动壳（缺 {runner}）。"
                f"启动壳必须由中台提供 —— 模块不允许自己决定监听地址。"
            )

        python = resolve_python(module_dir, spec.python)

        # ---- 端口探测：宁可在这里失败，也不要起了个连不上的进程
        if port_in_use(spec.internal_port):
            return self._attach_or_fail(
                runtime,
                why=f"内部端口 {spec.internal_port} 已被占用{_port_holder(spec.internal_port)}",
            )

        env = self._build_env(runtime, python)
        cmd = [str(python), str(runner), *[str(a) for a in spec.args]]

        runtime.log.append(f"[中台] {_now()} 启动 {spec.id}")
        runtime.log.append(f"[中台] 解释器：{python}")
        runtime.log.append(f"[中台] 工作目录：{module_dir}")
        runtime.log.append(f"[中台] 监听：127.0.0.1:{spec.internal_port}（地址由中台接管，非模块自选）")
        if not spec.allow_realtime:
            runtime.log.append(
                "[中台] 实时通道（socket.io / WebSocket）未放行 —— "
                "该通道上通常挂着任意命令执行能力，需另行配置白名单后才可开启"
            )

        creationflags = 0
        if _IS_WINDOWS:
            # 不要弹黑框；新进程组便于整组回收
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            process = subprocess.Popen(  # noqa: S603 - 命令由中台拼装，非用户输入
                cmd,
                cwd=str(module_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            runtime.state = "failed"
            runtime.error = f"拉起子进程失败：{exc}"
            raise ModuleStartError(runtime.error) from exc

        runtime.pid = process.pid
        runtime.attached = False
        runtime.started_at = _now()
        runtime.log.append(f"[中台] 子进程 PID {process.pid}")

        self._attach_log_pump(runtime, process)

        ok, detail = self._wait_healthy(runtime, process, wait)
        if ok:
            runtime.state = "running"
            runtime.health_checked_at = _now()
            runtime.log.append(f"[中台] 健康检查通过（{detail}），对外路径 {spec.mount}/")
            return {
                "state": "running",
                "pid": process.pid,
                "health": detail,
                "message": f"{spec.name} 已就绪",
            }

        # 起来但没就绪：把子进程收掉，别留一个半死的进程占着端口
        runtime.state = "failed"
        runtime.error = detail
        runtime.log.append(f"[中台] 启动失败：{detail}")
        self._kill(runtime)
        raise ModuleStartError(detail)

    def _attach_or_fail(self, runtime: ModuleRuntime, *, why: str) -> dict:
        """端口已占用 / external 模块：先探活，通则「接管」，不通则明确报错。"""
        spec = runtime.spec
        ok, detail = _health_ok(spec.internal_port, spec.health_path)
        if ok:
            runtime.state = "running"
            runtime.attached = True
            runtime.pid = None
            runtime.started_at = runtime.started_at or _now()
            runtime.health_checked_at = _now()
            runtime.log.append(
                f"[中台] {why}；健康检查通过（{detail}），按「已就绪」接管。"
                f"注意：该进程不是中台这次拉起的，中台退出时也不会去收它。"
            )
            return {
                "state": "running",
                "attached": True,
                "health": detail,
                "message": f"{spec.name} 已在运行（非中台拉起），直接接管",
            }
        runtime.state = "failed"
        runtime.error = (
            f"{why}，且健康检查 {spec.health_path} 无响应（{detail}）。"
            f"请先释放端口，或在模块面板里点「重载声明」后重试。"
        )
        runtime.log.append(f"[中台] {runtime.error}")
        raise ModuleStartError(runtime.error)

    def _build_env(self, runtime: ModuleRuntime, python: Path) -> dict:
        spec = runtime.spec
        env = dict(os.environ)
        env.update(
            {
                "LOCALDECK_MODULE_ID": spec.id,
                "LOCALDECK_MODULE_ROOT": str(self._registry_dir() / spec.id),
                # 模块内的工作目录（相对模块根）。给「包藏在子目录里」的项目用：
                # portal 的 Python 包在 backend/ 下，启动壳得先 chdir 过去再 import。
                "LOCALDECK_MODULE_CWD": spec.app_cwd,
                "LOCALDECK_MODULE_PORT": str(spec.internal_port),
                "LOCALDECK_MODULE_APP_MODULE": spec.app_module,
                "LOCALDECK_MODULE_APP_OBJECT": spec.app_object,
                "LOCALDECK_MODULE_SOCKETIO_OBJECT": spec.socketio_object,
                # 中台自己的设置绝不能漏进子进程
                "LOCALDECK_TOKEN": "",
                "LOCALDECK_DATA_DIR": str(config.MODULE_LOG_DIR / "_unused"),
                # 很多 Flask 应用直接读 PORT，一并给上，省得它们退回默认端口
                "PORT": str(spec.internal_port),
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        env.update({str(k): str(v) for k, v in (spec.env or {}).items()})
        # 共享凭据：让模块能分辨「这个请求确实经中台代理而来」。
        #
        # 用途是给模块一个**可验证**的免登录依据。只靠 `X-Forwarded-Prefix`
        # 这类头是不够的 —— 本机任何进程都能伪造它。而票据是每次运行新生成的
        # 随机串，只出现在中台与模块之间。
        #
        # 独立运行（不经中台启动、直接 `python app.py`）时这个变量根本不存在，
        # 模块于是只能走正常登录 —— 这正是想要的默认：**没有上游边界时绝不免登录**。
        # 放在 spec.env 之后覆盖，模块自己声明不了它。
        env["LOCALDECK_MODULE_TICKET"] = runtime.ticket
        # 声明里写的 host 类变量即使绕过校验，也在最后一道按死
        for key in list(env):
            if key.lower() in {"host", "bind", "listen"}:
                env.pop(key, None)
        return env

    def _attach_log_pump(self, runtime: ModuleRuntime, process: subprocess.Popen) -> None:
        log_path = config.MODULE_LOG_DIR / f"{runtime.spec.id}.log"
        try:
            # buffering=1 是行缓冲。少了它，日志会攒在 Python 的缓冲区里，
            # 模块还在跑的时候去翻这个文件会以为「什么都没输出」——
            # 而模块日志恰恰是排障时最先要看的东西。
            handle = log_path.open("a", encoding="utf-8", buffering=1)
        except OSError:
            handle = None

        if handle:
            handle.write(
                f"\n===== {_now()} 启动 {runtime.spec.id} "
                f"(PID {process.pid}) =====\n"
            )
            handle.flush()

        def pump() -> None:
            stream = process.stdout
            if stream is not None:
                for line in stream:
                    text = line.rstrip("\n")
                    runtime.log.append(text)
                    if handle:
                        try:
                            handle.write(text + "\n")
                        except (OSError, ValueError):
                            pass
            if handle:
                try:
                    handle.flush()
                    handle.close()
                except (OSError, ValueError):
                    pass
            code = process.wait()
            # 只有「中台以为它在跑、它却退了」才需要改状态；
            # 正常 stop() 时状态已经被置成 stopping/stopped，别覆盖掉。
            with self._lock:
                if runtime.state in {"running", "starting"}:
                    runtime.state = "failed"
                    runtime.error = f"子进程意外退出，退出码 {code}（详见模块日志）"
                    runtime.finished_at = _now()
                    runtime.pid = None
                    runtime.log.append(f"[中台] 子进程退出，退出码 {code}")

        threading.Thread(target=pump, name=f"modlog-{runtime.spec.id}", daemon=True).start()

    def _wait_healthy(
        self, runtime: ModuleRuntime, process: subprocess.Popen, wait: float
    ) -> tuple[bool, str]:
        spec = runtime.spec
        deadline = time.monotonic() + max(wait, 3.0)
        last = "尚未响应"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = " | ".join(list(runtime.log)[-4:])
                return False, f"子进程启动后立即退出（退出码 {process.returncode}）：{tail}"
            ok, detail = _health_ok(spec.internal_port, spec.health_path)
            if ok:
                return True, detail
            last = detail
            time.sleep(0.4)
        return False, (
            f"{wait:.0f} 秒内 {spec.health_path} 始终未就绪（最后一次：{last}）。"
            f"常见原因：模块依赖没装在它自己的 venv 里、或端口被防火墙挡下。"
            f"到「模块」面板看日志尾部最直接。"
        )

    # -------------------------------------------------------- 停止
    def stop(self, module_id: str, *, wait: float = 12.0) -> dict:
        runtime = self._registry.runtime(module_id)
        if runtime is None:
            raise ModuleStartError(f"没有这个模块：{module_id}")
        with self._lock:
            if runtime.state == "stopped":
                return {"state": "stopped", "message": "本来就停着"}
            if runtime.attached:
                return {
                    "state": "running",
                    "attached": True,
                    "message": (
                        "这个进程不是中台拉起的（端口接管而来），中台不会替你杀掉它。"
                        "需要停就自己到任务管理器里按 PID 结束。"
                    ),
                }
            runtime.state = "stopping"
            return self._kill(runtime, wait=wait)

    def _kill(self, runtime: ModuleRuntime, *, wait: float = 12.0) -> dict:
        pid = runtime.pid
        if not pid:
            runtime.state = "stopped"
            runtime.pid = None
            runtime.finished_at = _now()
            return {"state": "stopped", "message": "没有记录到子进程，已标记为停止"}

        runtime.log.append(f"[中台] 终止进程树 (PID {pid})")
        _terminate_tree(pid)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not port_in_use(runtime.spec.internal_port):
                break
            time.sleep(0.3)

        still = port_in_use(runtime.spec.internal_port)
        runtime.pid = None
        runtime.attached = False
        runtime.finished_at = _now()
        if still:
            runtime.state = "failed"
            runtime.error = f"已发出终止指令，但端口 {runtime.spec.internal_port} 仍被占用"
            runtime.log.append(f"[中台] {runtime.error}")
            return {"state": "failed", "message": runtime.error}
        runtime.state = "stopped"
        runtime.log.append("[中台] 已停止")
        return {"state": "stopped", "message": "已停止"}

    def restart(self, module_id: str, *, timeout: float | None = None) -> dict:
        runtime = self._registry.runtime(module_id)
        if runtime is None:
            raise ModuleStartError(f"没有这个模块：{module_id}")
        self.stop(module_id)
        return self.start(module_id, timeout=timeout)

    # -------------------------------------------------------- 批量
    def autostart_async(self) -> None:
        """中台起来后，在后台把该自动启动的模块拉起来。

        刻意不放在 lifespan 里同步等待：OpsGen 首次冷启动要装依赖、跑
        werkzeug，把中台首页卡在它后面是没道理的。前端会实时显示「启动中」。
        """

        def worker() -> None:
            for spec in self._registry.all():
                if spec.kind not in {"subprocess_proxy", "external"}:
                    continue
                if not (spec.enabled and spec.auto_start):
                    continue
                try:
                    self.start(spec.id)
                except ModuleStartError as exc:
                    runtime = self._registry.runtime(spec.id)
                    if runtime:
                        runtime.state = "failed"
                        runtime.error = str(exc)
                except Exception as exc:  # noqa: BLE001 - 后台线程不能因为一个模块崩掉
                    runtime = self._registry.runtime(spec.id)
                    if runtime:
                        runtime.state = "failed"
                        runtime.error = f"{type(exc).__name__}: {exc}"

        threading.Thread(target=worker, name="module-autostart", daemon=True).start()

    def shutdown(self) -> None:
        """中台退出时按相反顺序收走自己拉起的进程。"""
        for runtime in reversed(self._registry.runtimes()):
            if runtime.state in {"running", "starting"} and not runtime.attached:
                try:
                    self.stop(runtime.spec.id)
                except Exception:  # noqa: BLE001 - 退出路径上不抛异常
                    pass

    # -------------------------------------------------------- 杂
    def _registry_dir(self) -> Path:
        return self._registry._dir  # noqa: SLF001 - 同一功能域内的两个组件，直取省一层转发
