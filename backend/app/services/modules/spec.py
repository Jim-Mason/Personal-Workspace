"""模块声明（ModuleSpec）。

每个模块在自己的目录下放一个 `module.py`，里面只写一句：

    MODULE = ModuleSpec(id="opsgen", name="运维脚本生成", kind="subprocess_proxy", ...)

**为什么用「独立命名空间 exec」加载，而不是 import？**

模块目录必须能脱离中台源码独立存在 —— 第三方模块（OpsGen 那种）随时会被
`git pull` 换成上游新版，它自己并不知道中台包叫什么、装在哪里。如果加载
方式要求 `from core.modules import ModuleSpec`，那就得先把中台塞进 `sys.path`，
等于把「模块」和「中台」在导入期就绑死，将来想把模块单独拷走都拷不动。

所以加载器这样办：把 `ModuleSpec` 这个名字直接注入一个干净的命名空间，
再把 `module.py` 的源码 exec 进去。`module.py` 里那句 `ModuleSpec(...)`
照样能解析到，但它既没 import 过中台、中台的包也没进过它的 `sys.modules`。

顺带两个好处：
- `module.py` 想在别处被读取（比如生成文档）时不会触发整个中台 import
- 权限边界更清楚：模块拿不到中台的模块对象

**同时兼容纯 dict 写法**（`MODULE = {...}`）。中台自己是自家代码，怎么写都行；
但第三方模块若不想依赖 `ModuleSpec` 这个类，用 dict 声明同样合法。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path

# 四种接入形态，语义见 docs/ARCHITECTURE.md
KINDS = ("native", "static", "subprocess_proxy", "external")

_KIND_LABELS = {
    "native": "同进程挂载",
    "static": "托管静态产物",
    "subprocess_proxy": "子进程 + 反代",
    "external": "仅反代（模块自行启动）",
}

# mount 只允许一级路径段：/opsgen 可以，/a/b 不行。
# 限制成一级是为了让「最长前缀匹配」不必处理歧义，也让代理改写规则简单可验。
_MOUNT_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]*$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PYTHON_RE = re.compile(r"^[A-Za-z0-9_.:/\\-]+(\.exe)?$")


class ModuleSpecError(Exception):
    """模块声明有问题。错误信息要直接能指出是哪个文件哪一项，别让人猜。"""


@dataclass
class ModuleSpec:
    # ---------------------------------------------------------- 身份
    id: str
    name: str
    description: str = ""
    version: str = ""
    # 导航排序；小的在前。留出间隔便于插队（10/20/……）。
    order: int = 50

    # ---------------------------------------------------------- 接入
    kind: str = "subprocess_proxy"
    mount: str = ""          # 留空则取 /<id>
    enabled: bool = True

    # ---------------------------------------------------------- subprocess_proxy / external
    # 启动器：中台提供的标准壳，不落到模块目录里去。
    # 目前有 flask_app（Flask / Flask-SocketIO，含 werkzeug）。
    runner: str = ""
    app_module: str = "app"      # 模块内要 import 的模块名
    app_object: str = "app"      # 其中的 WSGI/ASGI 应用对象
    socketio_object: str = ""    # 非空 → 用 socketio.run 启动（WSGI + Socket.IO）
    # 模块内的工作目录（相对模块根，留空＝模块根）。
    # 给「包藏在子目录里」的项目用：portal 的 Python 包在 backend/ 下，
    # 上游是以 backend 为 cwd 跑 uvicorn 的，启动壳得先 chdir 过去、
    # 再把该目录插进 sys.path，否则 import app.main 找不到。
    app_cwd: str = ""
    internal_port: int = 0       # 必填，且只绑 127.0.0.1
    health_path: str = "/"
    startup_timeout: float = 25.0
    # 中台起来后是否自动拉起。关掉的话就得在「模块」面板里手动点启动。
    auto_start: bool = True
    # 是否允许浏览器直连模块的实时通道（socket.io / WebSocket）。
    # 默认 False：OpsGen 的实时通道上挂着「在线执行脚本」，那等同于
    # 把中台变成一个任意命令执行器。要开必须自己配白名单 + 逐次确认。
    allow_realtime: bool = False
    # 是否允许模块页面加载外部 CDN 资源。默认 False，对应「零网络上传、无遥测」。
    allow_external_assets: bool = False
    # 是否放行模块自有的 Cookie 会话（登录态）。
    #
    # 默认 False：OpsGen 那种没有登录的模块，让 Cookie 一概不流动最干净 ——
    # 少一条「模块偷偷维护了一套平行会话」的暗路。
    #
    # 但像 portal 这种自带 JWT 登录体系的完整应用，不放行就等于登录不上
    # （登录接口刚发的 Cookie，下一个请求就被剥掉了）。
    #
    # 注意：放行的是**模块自己的** Cookie。中台的模块票据（pw_mod_*）
    # 无论开关如何都不会流向模块 —— 平台凭据不进第三方进程，这条不让步。
    allow_module_cookies: bool = False
    env: dict = field(default_factory=dict)
    args: list = field(default_factory=list)
    # 模块私有解释器；留空则按约定找 modules/<id>/.venv，再退回中台自己的解释器。
    python: str = ""
    # 模块私有依赖清单，供 scripts/setup-modules.bat 使用
    requirements: str = "requirements.txt"

    # ---------------------------------------------------------- native / static
    router_object: str = ""       # native：模块里那个 APIRouter 对象名（配合 app_module）
    static_dir: str = ""          # static：相对模块目录的产物目录，默认 static

    # ---------------------------------------------------------- 合规与说明
    source_url: str = ""          # 上游仓库地址。MIT/Apache 类许可要求保留出处
    license: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.mount:
            self.mount = f"/{self.id}"

    # -------------------------------------------------------------- 校验
    def validate(self, origin: str) -> None:
        """把配置错误挡在启动阶段。

        这些检查放在这里而不是散在各处调用点：模块声明是最容易写错的地方
        （尤其 internal_port 撞车），一次性集中校验能省掉大量「起了但访问不到」
        的排查时间。
        """
        where = f"{origin}"
        if not _ID_RE.match(self.id or ""):
            raise ModuleSpecError(f"{where}: id 只能是小写字母/数字/下划线/短横线，当前为 {self.id!r}")
        if not self.name:
            raise ModuleSpecError(f"{where}: 缺少 name（导航里显示什么）")
        if self.kind not in KINDS:
            raise ModuleSpecError(f"{where}: kind={self.kind!r} 不认识，可选 {'/'.join(KINDS)}")
        if not _MOUNT_RE.match(self.mount or ""):
            raise ModuleSpecError(
                f"{where}: mount 必须是一级路径（形如 /opsgen），当前为 {self.mount!r}"
            )
        # 中台自身的命名空间不许被模块占用，否则代理会互相打架
        if self.mount in {"/api", "/static", "/index.html"}:
            raise ModuleSpecError(f"{where}: mount={self.mount} 与中台保留路径冲突")
        if not isinstance(self.order, int):
            raise ModuleSpecError(f"{where}: order 必须是整数")

        if self.kind in {"subprocess_proxy", "external"}:
            if not (1 <= int(self.internal_port or 0) <= 65535):
                raise ModuleSpecError(
                    f"{where}: 必须给一个 1024–65535 之间的 internal_port（内部端口只绑 127.0.0.1）"
                )
            if self.internal_port < 1024:
                raise ModuleSpecError(f"{where}: internal_port 不要用特权端口（<1024）")
            if not self.health_path.startswith("/"):
                raise ModuleSpecError(f"{where}: health_path 必须以 / 开头")
            if self.python and not _PYTHON_RE.match(self.python):
                raise ModuleSpecError(f"{where}: python 路径含非法字符：{self.python!r}")
        if self.app_cwd:
            # 启动壳会拿它去 chdir 并插进 sys.path，所以必须限定在模块目录内。
            #
            # 判断顺序要紧：**先判绝对路径，再去两侧斜杠**。反过来写的话，
            # '/abs' 会先被 strip 成 'abs' 混过检查 —— 而路径拼接时
            # `Path(root) / '/abs'` 在 pathlib 里是真会跳到根目录去的。
            raw = self.app_cwd.replace("\\", "/").strip()
            looks_absolute = raw.startswith("/") or bool(re.match(r"^[A-Za-z]:", raw))
            parts = [piece for piece in raw.strip("/").split("/") if piece]
            if not parts or looks_absolute or any(piece == ".." for piece in parts):
                raise ModuleSpecError(
                    f"{where}: app_cwd 必须是模块目录内的相对路径"
                    f"（不能是绝对路径、不能含 ..），当前为 {self.app_cwd!r}"
                )

        if self.kind == "subprocess_proxy" and not self.runner:
            raise ModuleSpecError(
                f"{where}: kind=subprocess_proxy 需要 runner（由中台提供的启动壳），"
                f"否则就等于让第三方模块自己决定监听地址 —— 那是不允许的"
            )
        if self.kind == "native" and not self.router_object:
            raise ModuleSpecError(f"{where}: kind=native 需要 router_object（模块里 APIRouter 的对象名）")

        for key in self.env or {}:
            if not isinstance(key, str) or not key:
                raise ModuleSpecError(f"{where}: env 的键必须是字符串")
            if key.lower() in {"host", "bind", "listen", "flask_run_host"}:
                raise ModuleSpecError(
                    f"{where}: 不允许模块自己指定监听地址（env 里的 {key}）"
                    f"—— 监听地址由中台统一接管，恒为 127.0.0.1"
                )

    # -------------------------------------------------------------- 展示
    def to_public(self) -> dict:
        """给前端的字段。刻意不含 env / args / python —— 那些是实现细节，
        而且可能带路径，没必要吐到浏览器里。

        `internal_port` 是故意开放的：它只绑在 127.0.0.1 上，写出来
        只是为了排障时能一眼对上是哪个进程（`netstat | findstr 8732`）。
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "kind": self.kind,
            "kind_label": _KIND_LABELS.get(self.kind, self.kind),
            "mount": self.mount,
            "order": self.order,
            "enabled": self.enabled,
            "internal_port": self.internal_port or None,
            "source_url": self.source_url,
            "license": self.license,
            "note": self.note,
            "allow_realtime": self.allow_realtime,
            "allow_external_assets": self.allow_external_assets,
            "allow_module_cookies": self.allow_module_cookies,
        }


# exec 时注入的名字。除它之外不注入任何中台符号。
_EXEC_NAMESPACE = ("ModuleSpec",)

_FIELD_NAMES = {f.name for f in fields(ModuleSpec)}


def load_spec(module_dir: Path) -> ModuleSpec:
    """读取并校验 `modules/<id>/module.py`。

    用 exec 而不是 importlib，理由见模块 docstring。
    """
    path = module_dir / "module.py"
    if not path.exists():
        raise ModuleSpecError(f"{module_dir} 下没有 module.py —— 模块必须声明自己怎么接入")

    source = path.read_text(encoding="utf-8")
    namespace: dict = {
        "__name__": f"localdeck_modules.{module_dir.name}",
        "__file__": str(path),
        "__package__": None,
        "ModuleSpec": ModuleSpec,
    }
    try:
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102 - 见 docstring
    except Exception as exc:  # noqa: BLE001
        raise ModuleSpecError(f"{path}: 加载失败 —— {type(exc).__name__}: {exc}") from exc

    declared = namespace.get("MODULE")
    if declared is None:
        raise ModuleSpecError(f"{path}: 没有找到 MODULE 声明（应为 ModuleSpec(...) 或等价的 dict）")

    spec = _coerce(declared, path, module_dir)
    spec.validate(str(path))
    if spec.id != module_dir.name:
        raise ModuleSpecError(
            f"{path}: id={spec.id!r} 与目录名 {module_dir.name!r} 不一致。"
            f"id 与目录同名，才能在日志、数据目录、审计流水里一眼对上。"
        )
    return spec


def _coerce(declared: object, path: Path, module_dir: Path) -> ModuleSpec:
    """把声明统一成 ModuleSpec。允许 ModuleSpec 实例、dataclass 等价对象或纯 dict。"""
    if isinstance(declared, ModuleSpec):
        return declared

    if isinstance(declared, dict):
        unknown = set(declared) - _FIELD_NAMES
        if unknown:
            raise ModuleSpecError(
                f"{path}: MODULE 里有中台不认识的字段 {sorted(unknown)}，"
                f"写错的字段会被静默忽略，所以这里直接报错"
            )
        try:
            return ModuleSpec(**declared)
        except TypeError as exc:
            raise ModuleSpecError(f"{path}: MODULE 字段有误 —— {exc}") from exc

    # 兼容「别人用 dataclasses.replace 之类的等价对象」的情况
    payload = {name: getattr(declared, name) for name in _FIELD_NAMES if hasattr(declared, name)}
    if not payload:
        raise ModuleSpecError(f"{path}: MODULE 必须是 ModuleSpec(...) 或 dict")
    return ModuleSpec(**payload)
