"""模块中枢：注册表 + 子进程 + 反向代理，对外只暴露一个装配入口。

分层是刻意的：

- `spec`       —— 模块怎么写声明（纯数据，可直接单测）
- `registry`   —— 有哪些模块、现在什么状态（纯内存，无 IO 副作用）
- `supervisor` —— 进程的拉起与回收（唯一会 spawn 的地方）
- `rewrite`    —— 内容改写规则（纯字符串函数，可脱离 HTTP 单测）
- `proxy`      —— HTTP 转发与会话（唯一会发请求的地方）
- `hub`        —— 把上面四个拼起来，并挂到 FastAPI 上

这样拆的好处很实在：出问题时能立刻定位到是哪一层的责任 ——
页面上路径错乱是 rewrite 的锅，进程起不来是 supervisor 的锅，
鉴权不通是 proxy/security 的锅，不用在一个大文件里翻。
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

from fastapi import FastAPI

from ... import config
from ...security import NO_MODULE, ModuleCredential
from .proxy import ModuleProxy
from .registry import ModuleRegistry, ModuleRuntime
from .spec import ModuleSpec, ModuleSpecError
from .supervisor import ModuleStartError, ModuleSupervisor

#: native 模块共同的父包名。每个模块以 `localdeck_modules.<id>` 的身份被导入，
#: 于是两个模块各有一个 `store.py` 也互不干扰。理由见 `_import_object`。
_NATIVE_ROOT_PACKAGE = "localdeck_modules"


class ModuleHub:
    def __init__(self) -> None:
        self.registry = ModuleRegistry(config.MODULES_DIR)
        self.supervisor = ModuleSupervisor(self.registry)
        self.proxy = ModuleProxy(self.registry)
        self._native_mounted: set[str] = set()
        self._static_mounted: set[str] = set()
        self.errors: dict[str, str] = self.registry.errors()

    # ------------------------------------------------------------ 生命周期
    async def startup(self) -> None:
        """只在服务真正开始跑之后调用。

        native / static 的挂载**不在这里** —— 那两类的路由必须在服务开始
        接受请求之前就挂好，所以放在 create_app() 里同步做掉。
        这里只负责把子进程拉起来。
        """
        config.MODULE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        # subprocess_proxy 放后台拉，别把中台首页卡在模块冷启动后面
        self.supervisor.autostart_async()

    async def shutdown(self) -> None:
        self.supervisor.shutdown()
        await self.proxy.aclose()

    # ------------------------------------------------------------ 同进程接入
    def mount_native(self, app: FastAPI) -> list[str]:
        """挂载 kind=native 的模块（模块提供 FastAPI APIRouter，中台 include_router）。"""
        mounted: list[str] = []
        for spec in self.registry.all():
            if spec.kind != "native" or not spec.enabled or spec.id in self._native_mounted:
                continue
            module_dir = config.MODULES_DIR / spec.id
            try:
                # 先交上下文再导入：模块的 context.py 是在 import 期读环境的，
                # 顺序反了它就会退回自己目录的 data/，静默连到另一个库上。
                self._export_native_context(spec)
                router = self._import_object(module_dir, spec.app_module, spec.router_object)
                if hasattr(router, "routes"):  # APIRouter
                    app.include_router(router, prefix=spec.mount)
                else:  # 任意 ASGI 可调用
                    app.mount(spec.mount, router)
            except Exception as exc:  # noqa: BLE001 - 一个模块挂不上不该拖垮中台
                self.errors[spec.id] = f"native 挂载失败：{type(exc).__name__}: {exc}"
                runtime = self.registry.runtime(spec.id)
                if runtime:
                    runtime.state = "failed"
                    runtime.error = self.errors[spec.id]
                continue
            self._native_mounted.add(spec.id)
            mounted.append(spec.id)
        return mounted

    def mount_static(self, app: FastAPI) -> list[str]:
        """挂载 kind=static 的模块（中台托管它的前端构建产物，只读）。"""
        from fastapi.staticfiles import StaticFiles

        mounted: list[str] = []
        for spec in self.registry.all():
            if spec.kind != "static" or not spec.enabled or spec.id in self._static_mounted:
                continue
            folder = config.MODULES_DIR / spec.id / (spec.static_dir or "static")
            if not folder.is_dir():
                self.errors[spec.id] = f"static 产物目录不存在：{folder}"
                continue
            app.mount(
                spec.mount,
                # html=True 让 /portal/ 自动落到 index.html，SPA 的深链接才有救
                StaticFiles(directory=str(folder), html=True),
                name=f"module-{spec.id}",
            )
            self._static_mounted.add(spec.id)
            mounted.append(spec.id)
        return mounted

    def _export_native_context(self, spec: ModuleSpec) -> None:
        """把 native 模块需要的那点上下文经环境变量交给它。

        为什么不是直接 import 中台的包：模块要能**脱离中台源码单独存在** ——
        同一份代码也得能在它自己目录里跑起来调试。走环境变量是最小、最中立的
        契约，模块侧的 `context.py` 读不到就退回自己目录下的 `data/`。

        这里给的是**只读的路径信息**，不含令牌、不含任何凭据 ——
        native 模块与中台同进程，但它不该顺手拿到平台的秘密。
        """
        config.ensure_dirs()
        module_root = config.MODULES_DIR / spec.id
        os.environ["LOCALDECK_DATA_DIR"] = str(config.DATA_DIR)
        os.environ["LOCALDECK_DB_PATH"] = str(config.DB_PATH)
        os.environ["LOCALDECK_MODULE_ROOT"] = str(module_root)
        os.environ["LOCALDECK_MODULE_ID"] = spec.id
        # 模块**自己的**数据目录（各模块数据各归各家，中台不越权读写）。
        # 与 LOCALDECK_DATA_DIR 是两回事：后者是共享库所在的中台数据目录，
        # 只有真正需要写平台级表（如模块一）的模块才该用它。
        os.environ["LOCALDECK_MODULE_DATA_DIR"] = str(module_root / "data")
        # 挂载点。自带页面的模块要靠它拼自己的资源与接口地址 ——
        # 写死在 HTML 里的话，哪天换个挂载点页面就整片 404。
        os.environ["LOCALDECK_MOUNT"] = spec.mount
        # 机器标识要在中台这边生成好再给它：两边各生成一次会得到两个不同的值，
        # 那样写进 task 表的 machine_id 就对不上了，多机归因直接失效。
        try:
            os.environ["LOCALDECK_MACHINE_ID"] = config.get_or_create_machine_id()
        except Exception:  # noqa: BLE001 - 拿不到就让它自己去读文件
            pass

    def _import_object(self, module_dir: Path, module_name: str, object_name: str):
        """按模块自己的目录导入它的某个对象。

        **每个模块是一个独立的包**：`localdeck_modules.<id>`。父包是这里现造的
        命名空间包，`__path__` 指向 `modules/`。

        为什么不沿用「把模块目录塞进 sys.path，然后扁平 import」：
        那样 `import store` 会往 `sys.modules` 里塞一个**顶层**名叫 `store` 的
        模块 —— 名字这么泛，两个模块各有一个 `store.py` 就会互相顶掉，
        而且现象极难归因（拿到的是另一个模块的函数，报错现场却在业务逻辑里）。
        装进各自的包之后，模块之间天然隔离，模块内部也能正常写相对导入
        （`from . import store`），照着「一个包该长什么样」写就行。
        """
        package_dir = str(config.MODULES_DIR)
        if package_dir not in sys.path:
            sys.path.insert(0, package_dir)
        if _NATIVE_ROOT_PACKAGE not in sys.modules:
            root = types.ModuleType(_NATIVE_ROOT_PACKAGE)
            root.__path__ = [package_dir]  # type: ignore[attr-defined]
            sys.modules[_NATIVE_ROOT_PACKAGE] = root

        dotted = f"{_NATIVE_ROOT_PACKAGE}.{module_dir.name}.{module_name}"
        module = importlib.import_module(dotted)
        obj = getattr(module, object_name, None)
        if obj is None:
            raise ModuleSpecError(f"{dotted} 里没有 {object_name!r}")
        return obj

    # ------------------------------------------------------------ 鉴权对接
    def ticket_lookup(self, path: str) -> ModuleCredential:
        """安全中间件的回调：这个路径归哪个模块管、票据是什么、挂在哪、是不是自家模块。

        返回 NO_MODULE 表示「不是模块路径」，此时模块凭据通道关闭，
        必须走中台令牌。这样票据的作用域天然就被限制在各自的 mount 里。

        **挂载点**是给「票据换 Cookie」用的 —— 中间件要用它拼出 `Path=<mount>`，
        把 Cookie 的可见范围钉死在模块自己的路径下。

        **native** 是给「浏览器会话能不能走模块路径」用的：只有中台自己写的
        模块才认会话，第三方模块不认。理由见 `security.py` 里那段说明。
        """
        found = self.registry.by_mount(path)
        if found is None:
            return NO_MODULE
        spec, _rest = found
        runtime = self.registry.runtime(spec.id)
        if runtime is None:
            return NO_MODULE
        return ModuleCredential(
            ticket=runtime.ticket,
            cookie_name=runtime.cookie_name,
            mount=spec.mount,
            native=spec.kind == "native",
        )

    # ------------------------------------------------------------ 查询
    def public_list(self) -> dict:
        items = []
        for runtime in self.registry.runtimes():
            spec = runtime.spec
            entry = spec.to_public()
            entry.update(runtime.snapshot())
            entry.pop("log_tail", None)
            entry["open_url"] = self.open_url(spec, runtime)
            entry["proxy_ready"] = self._proxy_ready(spec, runtime)
            items.append(entry)
        return {
            "items": items,
            "errors": self.errors,
        }

    def open_url(self, spec: ModuleSpec, runtime: ModuleRuntime) -> str:
        """模块在中台下的入口地址。

        带上票据参数，且**只在首次进入时**用它 —— 拿到页面后中台会种一个
        HttpOnly 的 Cookie（由安全中间件统一下发，见 `security.install_security`），
        后面的子资源请求就不需要它了。
        `?_t=` 是模块级票据，不是中台令牌；即使第三方页面的脚本把它读走，
        也只能访问这个模块自己的路径。

        **四种接入形态一视同仁**：native / static 同样需要票据 —— 它们的页面
        也要过中台的鉴权，不带票据就是 401。
        """
        if runtime.ticket:
            return f"{spec.mount}/?_t={runtime.ticket}"
        return f"{spec.mount}/"

    @staticmethod
    def _proxy_ready(spec: ModuleSpec, runtime: ModuleRuntime) -> bool:
        if spec.kind in {"native", "static"}:
            return spec.enabled
        return runtime.state == "running"

    # ------------------------------------------------------------ 动作
    def reload(self) -> dict:
        result = self.registry.reload()
        self.errors = dict(result["errors"])
        # native 模块是在中台启动时挂进路由表的，运行期加进来的需要重挂
        for spec in self.registry.all():
            if spec.kind == "native":
                self._native_mounted.discard(spec.id)
        return result

    def start(self, module_id: str) -> dict:
        return self.supervisor.start(module_id)

    def stop(self, module_id: str) -> dict:
        return self.supervisor.stop(module_id)

    def restart(self, module_id: str) -> dict:
        return self.supervisor.restart(module_id)

    def log_tail(self, module_id: str, lines: int = 200) -> dict:
        runtime = self.registry.runtime(module_id)
        if runtime is None:
            raise ModuleStartError(f"没有这个模块：{module_id}")
        return {
            "id": module_id,
            "state": runtime.state,
            "lines": list(runtime.log)[-lines:],
            "log_file": str(config.MODULE_LOG_DIR / f"{module_id}.log"),
        }
