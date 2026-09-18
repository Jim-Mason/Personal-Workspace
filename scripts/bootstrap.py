#!/usr/bin/env python3
"""Personal Workspace 启动引导：建运行环境 → 装依赖 → 准备模块 → 起服务。

为什么这段逻辑在 Python 里，而不是留在 .bat 里
--------------------------------------------------------------------
cmd.exe 解码 .bat 用的是控制台代码页，而它对行尾也很敏感。UTF-8 无 BOM、
LF 行尾、外加中文注释这三样凑在一起时，它会算错行偏移，把注释里的词当成
命令去执行。实测报出来的是这样一串：

    'newer:' 不是内部或外部命令
    'Then'   不是内部或外部命令
    'See'    不是内部或外部命令

而报错的位置和真正出问题的行毫无关系 —— 看报错根本无从下手。

所以 .bat 现在只剩一个二十来行的纯 ASCII 骨架（连 chcp 都不放，免得再触发
重读偏移），提示与流程全部搬到这里：编码问题一次性消失，流程也终于能被测试
（`--modules-only` 就是给自检用的纯逻辑入口，不启动服务）。

两种运行身份
--------------------------------------------------------------------
· 首次运行：由系统 Python 执行 —— 这时 .venv 还没建起来
· 之后：由 .venv 里的 Python 执行 —— start.bat 优先选它，
  于是系统 Python 被升级或卸载也不影响中台启动

跑法
--------------------------------------------------------------------
    scripts\\bootstrap.py                   完整流程，最后启动服务
    scripts\\bootstrap.py --modules-only    只准备环境，不起服务
    scripts\\bootstrap.py --only opsgen     只处理某个模块
    scripts\\bootstrap.py --force           连已存在的模块环境也重建
    scripts\\bootstrap.py --no-browser      透传给 run.py：不自动开浏览器
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
VENV_PY = VENV_DIR / "Scripts" / "python.exe"
BACKEND_DIR = ROOT / "backend"
RUN_PY = BACKEND_DIR / "run.py"
REQUIREMENTS = BACKEND_DIR / "requirements.txt"
MODULES_DIR = ROOT / "modules"

WIDE = "=" * 64
THIN = "-" * 64

OK = 0
FAIL = 1


# --------------------------------------------------------------------- 输出


def say(text: str = "") -> None:
    print(text, flush=True)


_ORIGINAL_CONSOLE_CP: int | None = None


def fix_console_encoding() -> None:
    """让控制台按 UTF-8 解释本进程的输出。

    刻意不依赖 .bat 里的 `chcp` —— cmd 在 chcp 之后重读批处理文件时可能算错
    偏移，那正是我们要躲开的坑。这里直接改控制台代码页，效果一样，但不碰
    bat 的解析过程。

    退出前会把代码页还原：用户可能是从一个已经开着的 cmd 窗口里跑这个脚本的，
    不该因为我们改掉他控制台的设置。
    """
    global _ORIGINAL_CONSOLE_CP
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        _ORIGINAL_CONSOLE_CP = kernel32.GetConsoleOutputCP()
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        atexit.register(_restore_console_cp)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _restore_console_cp() -> None:
    if _ORIGINAL_CONSOLE_CP is None:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(_ORIGINAL_CONSOLE_CP)
        ctypes.windll.kernel32.SetConsoleCP(_ORIGINAL_CONSOLE_CP)
    except Exception:
        pass


def title(text: str) -> None:
    say()
    say(WIDE)
    say(f"  {text}")
    say(WIDE)


def fail(message: str, hints: tuple[str, ...] = ()) -> int:
    say()
    say(f"  [X] {message}")
    for hint in hints:
        say(f"      {hint}" if hint else "")
    say()
    return FAIL


# ----------------------------------------------------------------- 步骤 1~2


def step_runtime() -> int:
    say("[1/4] 运行环境")
    if VENV_PY.is_file():
        say("      已就绪")
        return OK

    say("      首次运行，正在创建私有运行环境（只做这一次）...")
    rc = subprocess.call([sys.executable, "-m", "venv", str(VENV_DIR)])
    if rc != 0 or not VENV_PY.is_file():
        return fail(
            "创建运行环境失败。",
            (
                "常见原因是 Python 安装不完整（缺 venv 模块）。",
                "建议重装 Python 3.11 及以上版本，",
                '安装时记得勾选 "Add python.exe to PATH"。',
            ),
        )
    say(f"      创建完成 {VENV_DIR}")
    return OK


def pip_install(python: Path, requirements: Path, label: str) -> bool:
    say(f"      安装依赖：{label}")
    rc = subprocess.call(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-q",
            "-r",
            str(requirements),
        ]
    )
    return rc == 0


def step_platform_deps() -> int:
    say("[2/4] 中台依赖")
    if not REQUIREMENTS.is_file():
        return fail(f"找不到依赖清单：{REQUIREMENTS}")

    first_time = not (VENV_DIR / "Lib" / "site-packages" / "uvicorn").is_dir()
    if first_time:
        say("      首次安装，需要下载依赖，请稍候（这一步最慢）")

    if not pip_install(VENV_PY, REQUIREMENTS, "backend/requirements.txt"):
        return fail(
            "中台依赖安装失败。",
            (
                "如果卡在网络上，先设代理再重跑，例如：",
                "  set HTTPS_PROXY=http://127.0.0.1:7890",
            ),
        )
    say("      就绪")
    return OK


# ------------------------------------------------------------------- 步骤 3


def module_dirs() -> list[Path]:
    """扫出 modules/<id>/module.py 这样的模块目录。

    只看声明文件在不在，不去 import 它 —— 这里是引导阶段，模块的依赖可能还
    没装，import 会直接炸。真正的声明校验在中台启动后由内核做。
    """
    if not MODULES_DIR.is_dir():
        return []
    return [
        item
        for item in sorted(MODULES_DIR.iterdir())
        if item.is_dir() and (item / "module.py").is_file()
    ]


def _load_spec_helpers():
    """直接按文件加载内核的 spec.py，拿到 ModuleSpec / load_spec。

    为什么不 `import app.services.modules.spec`：那个包的 `__init__` 会牵出
    hub，hub 又 import fastapi —— 而本脚本可能正跑在**还没装中台依赖**的系统
    解释器上（首次运行时就是这样）。

    spec.py 自己只用标准库，按文件加载绕开包初始化，就能既复用内核那套完全
    相同的声明校验、又不引入任何依赖 —— 免得两边各写一份规则、日后走岔。

    读不到就返回 None，调用方退回「模块根 requirements.txt」的朴素约定。
    """
    try:
        import importlib.util

        path = ROOT / "backend" / "app" / "services" / "modules" / "spec.py"
        if not path.is_file():
            return None
        loader = importlib.util.spec_from_file_location("_localdeck_spec", path)
        if loader is None or loader.loader is None:
            return None
        module = importlib.util.module_from_spec(loader)
        # 必须先注册进 sys.modules 再执行，否则加载会失败。
        #
        # 原因有点绕：spec.py 用了 `from __future__ import annotations`，所有注解
        # 都成了字符串；dataclasses 解析这些字符串时要回头查
        # sys.modules[cls.__module__] 来判定类型，而 module_from_spec 并**不会**
        # 把模块登记进去（这点和普通 import 不同）。拿不到就 AttributeError，
        # 报错位置还指向 spec.py 里的 @dataclass —— 跟真因（模块没注册）毫无关系。
        sys.modules[loader.name] = module
        loader.loader.exec_module(module)
        return module
    except Exception:
        return None


def _read_spec(helpers, module_dir: Path):
    """读模块声明。读不动不算致命 —— 环境准备照做，声明问题留给中台启动时报。"""
    if helpers is None:
        return None
    try:
        return helpers.load_spec(module_dir)
    except Exception as exc:  # noqa: BLE001
        say(f"      [!]   {module_dir.name}：声明有问题（{exc}）")
        say("            环境仍会按默认约定准备，中台启动时会再报一次")
        return None


def _requirements_path(module_dir: Path, declared) -> Path | None:
    """模块的依赖清单在哪。

    优先用声明里的 `requirements`（相对模块根）。像 portal 那种把包放在
    backend/ 下的项目，依赖清单也在 backend/ 下，光看模块根是找不到的。
    """
    relative = (getattr(declared, "requirements", "") or "requirements.txt").strip()
    candidate = (module_dir / relative).resolve()
    if candidate != module_dir and module_dir not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def base_interpreter() -> Path:
    """建模块 venv 时用的基础解释器。

    优先取中台运行环境所基于的那一个（.venv/pyvenv.cfg 里的 executable），
    让模块与中台的 Python 版本天然一致。否则可能模块跑在 3.14、中台跑在
    3.13，模块里能 import 的东西在中台侧复现不出来 —— 这种问题排查起来很费神。
    """
    cfg = VENV_DIR / "pyvenv.cfg"
    if cfg.is_file():
        for raw in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = raw.partition("=")
            if sep and key.strip().lower() == "executable":
                candidate = Path(value.strip())
                if candidate.is_file():
                    return candidate
    return Path(sys.executable)


def _frontend_notice(module_dir: Path) -> list[str]:
    """模块自带前端产物缺失时，给出**可以照抄**的构建命令。

    为什么必须有这一步：像 modules/portal 这种 SPA 模块，前端产物（dist/）
    **不进版本库**（模块自己的 .gitignore 就排除了它 —— 那是构建产物，
    提交它等于把编译结果当源码管）。于是新克隆下来的项目第一次启动时会出现
    最难归因的那种故障：**进程起来了、健康检查也过了、页面却是空的**。

    与其让人去猜，不如在引导阶段就把该跑的命令打出来。
    判据用「frontend/package.json 存在 且 dist/index.html 不存在」——
    按约定判断，不给 ModuleSpec 再加字段。
    """
    frontend = module_dir / "frontend"
    if not (frontend / "package.json").is_file():
        return []
    if (frontend / "dist" / "index.html").is_file():
        return []
    rel = frontend.relative_to(ROOT)
    return [
        f"      [!!]  {module_dir.name}：前端产物缺失 —— 模块能起来，但页面会是空的",
        f"            构建一次即可：",
        f"              cd {rel}",
        f"              npm install && npm run build",
            ]


def step_module_runtimes(*, force: bool, only: str | None) -> int:
    say("[3/4] 模块环境")

    targets = module_dirs()
    if only:
        targets = [item for item in targets if item.name == only]
        if not targets:
            return fail(f"modules\\{only}\\module.py 不存在。")

    if not targets:
        say("      modules\\ 下暂无模块，跳过")
        return OK

    base = base_interpreter()
    say(f"      基础解释器：{base}")

    helpers = _load_spec_helpers()
    built = skipped = failed = frontend_pending = 0

    for mdir in targets:
        name = mdir.name
        # 前端产物检查放在最前面且独立于依赖清单：有的模块可能没有 Python
        # 依赖（会在下面 continue 掉），但一样可能有前端产物要构建。
        for line in _frontend_notice(mdir):
            if line.lstrip().startswith("[!!]"):
                frontend_pending += 1
            say(line)

        declared = _read_spec(helpers, mdir)
        requirements = _requirements_path(mdir, declared)

        if requirements is None:
            say(f"      [--]  {name}：找不到依赖清单，跳过")
            skipped += 1
            continue

        mvenv = mdir / ".venv"
        mpy = mvenv / "Scripts" / "python.exe"

        if force and mvenv.is_dir():
            say(f"      [..]  {name}：--force，删掉旧环境重建")
            shutil.rmtree(mvenv, ignore_errors=True)

        if not mpy.is_file():
            say(f"      [1/2] {name}：建立私有环境 modules\\{name}\\.venv")
            rc = subprocess.call([str(base), "-m", "venv", str(mvenv)])
            if rc != 0 or not mpy.is_file():
                say(f"      [X]   {name}：建 venv 失败")
                failed += 1
                continue
        else:
            say(f"      [1/2] {name}：已有私有环境")

        label = str(requirements.relative_to(ROOT)).replace("\\", "/")
        if not pip_install(mpy, requirements, label):
            say(f"      [X]   {name}：装依赖失败")
            say("            卡在网络上就先设代理再重跑：")
            say("              set HTTPS_PROXY=http://127.0.0.1:7890")
            failed += 1
            continue

        say(f"      [OK]  {name}：就绪")
        built += 1

    say()
    say(THIN)
    tail = f"  建好 {built} 个，跳过 {skipped} 个，失败 {failed} 个"
    if frontend_pending:
        tail += f"，前端待构建 {frontend_pending} 个"
    say(tail)
    say(THIN)
    return FAIL if failed else OK


# ------------------------------------------------------------------- 步骤 4


def is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """端口上有没有东西在监听 —— 实际连一次来判断。

    刻意不依赖 psutil：主判据不能建立在一个可能没装的第三方包上。
    第一版就是栽在这里 —— 这个环境的 venv 里没有 psutil，`except ImportError`
    把整个端口检测静默吞成了"没占用"，于是用户看到的仍然只是 uvicorn 抛出来的
    一句英文 WinError 10048。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def port_holder(port: int) -> tuple[int, str] | None:
    """尽力查出占用者，只影响提示的详细程度，不影响要不要拦。

    先试 psutil（能直接拿到进程名），没有就退回系统自带的 netstat + tasklist。
    两条路都不通也只是提示得笼统一点，该拦还是拦得住。
    """
    return _holder_via_psutil(port) or _holder_via_netstat(port)


def _holder_via_psutil(port: int) -> tuple[int, str] | None:
    try:
        import psutil
    except Exception:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            if conn.laddr.port == port:
                name = "?"
                try:
                    name = psutil.Process(conn.pid).name()
                except Exception:
                    pass
                return conn.pid, name
    except Exception:
        return None
    return None


def _holder_via_netstat(port: int) -> tuple[int, str] | None:
    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return None

    suffix = f":{port}"
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        if not parts[1].endswith(suffix):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        return pid, _process_name(pid)
    return None


def _process_name(pid: int) -> str:
    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        first_line = output.strip().splitlines()[0] if output.strip() else ""
        return first_line.split(",")[0].strip('"') or "?"
    except Exception:
        return "?"


def step_launch(passthrough: list[str]) -> int:
    say("[4/4] 启动中台")

    port = int(os.environ.get("LOCALDECK_PORT", "8731"))
    if is_listening(port):
        holder = port_holder(port)
        if holder:
            pid, name = holder
            headline = f"端口 {port} 已被占用 —— PID {pid}（{name}）。"
            killer = f"    taskkill /F /T /PID {pid}"
        else:
            headline = f"端口 {port} 已被占用（占用者身份没查出来）。"
            killer = "    在任务管理器里按 PID 排序，找到监听该端口的进程结束它"

        return fail(
            headline,
            (
                "多半是已经有一个中台在跑：先关掉那个窗口。",
                "窗口关了但进程还在的话，结束它：",
                killer,
                "",
                "想两个实例同时跑，就换个端口：set LOCALDECK_PORT=8741",
            ),
        )

    say(f"      地址 http://127.0.0.1:{port}（稍后自动打开浏览器）")
    say(THIN)

    # PYTHONUNBUFFERED：子进程的 stdout 一旦被重定向（管道、日志文件），
    # 默认就变成块缓冲，于是它打印的启动横幅会晚于后发生的错误日志到达 ——
    # 排查时看着像"错误发生在横幅之前"，纯粹是假的。让它逐行吐。
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [str(VENV_PY), str(RUN_PY), *passthrough],
        cwd=str(BACKEND_DIR),
        env=env,
    )
    try:
        proc.wait()
    except KeyboardInterrupt:
        say()
        say("  正在停止...")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return proc.returncode or OK


# --------------------------------------------------------------------- 入口


def main(argv: list[str]) -> int:
    fix_console_encoding()

    if sys.version_info < (3, 9):
        return fail(
            f"需要 Python 3.9 及以上，当前是 {sys.version.split()[0]}。",
            (
                "请到 https://www.python.org/downloads/ 安装新版本。",
                '安装时记得勾选 "Add python.exe to PATH"。',
            ),
        )

    modules_only = "--modules-only" in argv
    force = "--force" in argv
    only: str | None = None
    passthrough: list[str] = []

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--modules-only", "--force"):
            pass
        elif arg == "--only" and index + 1 < len(argv):
            index += 1
            only = argv[index]
        else:
            passthrough.append(arg)
        index += 1

    title("Personal Workspace · 启动引导")
    say(f"  项目目录 : {ROOT}")
    say(f"  解释器   : {sys.executable}")

    for step in (step_runtime, step_platform_deps):
        rc = step()
        if rc != OK:
            return rc

    rc = step_module_runtimes(force=force, only=only)
    if rc != OK:
        return rc

    if modules_only:
        say()
        say("  完成（只准备了环境，按参数要求没有启动服务）。")
        return OK

    return step_launch(passthrough)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
