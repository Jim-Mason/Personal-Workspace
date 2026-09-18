"""模块一：本机资产清单（只读 + 归档）。

### 形态：`native`（同进程挂载）

它是本项目自己的代码，也不占端口 —— 直接以 `APIRouter` 挂进中台的 FastAPI
应用即可，不必起子进程。三条理由：

1. **不需要进程隔离**：没有 WebSocket、没有阻塞式长任务、没有自己的依赖集
   （只用标准库 + FastAPI）
2. **它写的是平台级的表**：`resource` / `task` / `event` 的表结构由中台的迁移
   文件定义，概览页也在读同一份数据。同进程挂载天然共享一个库，不必再开
   第二套连接与锁协商
3. **少一个端口、少一个进程**：能不起就不起

### 数据在哪

沿用中台的 `data/localdeck.db`，**表结构与数据一字未改**。模块通过中台注入的
环境变量拿到库路径（见 `context.py`），自己开连接 —— 这样它既不用 import
中台的包（保住了「模块可脱离中台存在」），又和平台看到同一份事实。

### 迁进来时对外行为零变化

路由前缀仍是 `/api/apps`，只是整体挂到了 `/inventory` 之下：

| 迁前 | 迁后 |
|---|---|
| `/api/apps` | `/inventory/api/apps` |
| `/api/apps/scan` | `/inventory/api/apps/scan` |
| `/api/apps/{id}/launch` | `/inventory/api/apps/{id}/launch` |
| `/api/apps/{id}` | `/inventory/api/apps/{id}` |

多出来的 `/inventory` 是模块命名空间，**这是刻意的**：模块不该有权占用中台的
`/api` 根路径，否则两个模块都想叫 `/api` 的时候就没人能仲裁了。

### 只读边界（这条不许放宽）

扫描全程只读：不写注册表、不移动、不删除、不改名任何文件。
启动动作只有两种 —— 用系统默认程序打开一个**已存在**的路径，或用默认浏览器
打开一个网址。**不提供任意命令执行**（见 `launcher.py` 顶部）。
"""

MODULE = ModuleSpec(
    id="inventory",
    name="本机资产清单",
    description=(
        "扫描本机已安装程序（注册表三处 + 开始菜单），卡片与表格双视图，"
        "可搜索、排序、收藏、忽略、写备注；全程只读，不联网"
    ),
    version="1.0",
    order=10,
    kind="native",
    app_module="inventory_api",
    router_object="router",
    license="自有项目（本项目自研，非第三方）",
    note=(
        "从内核迁入（原 backend/app/controllers/apps.py + services/），"
        "对外行为零变化，仅 URL 多一层 /inventory 前缀。"
        "数据仍写中台的 localdeck.db，表结构与数据未改。"
    ),
)
