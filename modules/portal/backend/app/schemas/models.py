"""Pydantic 请求/响应模型。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.entities import OPEN_MODE_DIRECT, OPEN_MODE_PROXY

OpenMode = Literal["direct", "proxy"]
Role = Literal["admin", "user"]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")
_ALLOWED_TARGET_SCHEMES = ("http://", "https://")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 卡片底色预设（空字符串 = 默认蓝色渐变）
BG_STYLES: tuple[str, ...] = ("", "default", "blue", "violet", "mint", "sand", "rose", "slate")

# 一张卡片最多挂几条环境地址。
# 20 是"够用且不至于把弹窗撑爆"的经验值：真实场景里一个系统顶多 prod/test/dev/灰
# 度/预发 五六套入口；给到 20 是为了让"一个图标归档一整条业务线"这种玩法也能成立。
MAX_CARD_ENDPOINTS = 20


def _clean_accent(value: str) -> str:
    """图标主色：只接受 #RRGGBB，统一小写；空串表示跟随预设。"""
    color = value.strip()
    if not color:
        return ""
    if not _HEX_COLOR_RE.match(color):
        raise ValueError("图标主色必须是 #RRGGBB 格式，例如 #2b6cf6")
    return color.lower()


def _clean_icon_url(value: str) -> str:
    """图标图片地址：只允许本站上传目录或 http(s) 外链，防止 javascript:/data: 注入。"""
    url = value.strip()
    if not url:
        return ""
    lowered = url.lower()
    if lowered.startswith("/uploads/") or lowered.startswith(_ALLOWED_TARGET_SCHEMES):
        return url
    raise ValueError("图标地址只允许 /uploads/ 开头的本站文件或 http(s) 外链")


def _clean_slug_optional(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    slug = value.strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("slug 只能包含小写字母、数字和连字符，且以字母或数字开头")
    return slug


def _clean_slug_required(value: str) -> str:
    slug = _clean_slug_optional(value)
    if slug is None:
        raise ValueError("slug 不能为空")
    return slug


# ----------------------------------------------------------------------
# 通用
# ----------------------------------------------------------------------
class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageOut(BaseModel):
    message: str = "ok"


# ----------------------------------------------------------------------
# 认证
# ----------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        return value.strip()


class UserOut(ORMModel):
    uid: str
    username: str
    display_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime


class SessionOut(BaseModel):
    user: UserOut
    access_expires_at: datetime
    refresh_expires_at: datetime


# ----------------------------------------------------------------------
# 卡片
# ----------------------------------------------------------------------
class CardEndpointIn(BaseModel):
    """一条环境地址（挂在卡片下）。

    一张卡片可以挂多条地址 —— 同一个系统往往有 prod / test / dev 多套入口，
    把同一个图标挂满这些入口，点卡片时弹列表选一个打开，比铺满几十张磁贴清爽，
    也更适合"一个业务图标归档一条业务线"。

    每条地址**自带** open_mode，所以同一张卡片可以"生产走跳板机代理、内网测试直连"：
    open_mode 属于地址而不属于卡片，是因为"这个地址只能在跳板机上访问"本身就是
    地址的属性，跟同一张卡片的其它地址没有关系。

    slug 是这条地址在网关里的入口标识（`/gw/{slug}/`），也是它和 cards.slug 共享的
    同一命名空间。新建时留空由后端生成；导出时带上，导入时优先复用，这样
    刷一遍配置不会把已经发出去的书签链接改掉。

    `from_attributes=True` 是为了让 `CardExportItem.model_validate(card_orm)` 能
    直接从 ORM 对象上把 `card.endpoints` 读出来 —— 导出走的就是这条路径。
    请求体是 JSON，永远按 dict 校验，因此这个开关对写入路径没有任何影响。
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=1024)
    open_mode: OpenMode = OPEN_MODE_DIRECT
    verify_tls: bool | None = None
    slug: str | None = Field(default=None, max_length=80)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        url = value.strip()
        if not url.lower().startswith(_ALLOWED_TARGET_SCHEMES):
            raise ValueError("地址必须以 http:// 或 https:// 开头")
        return url

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str | None) -> str | None:
        return _clean_slug_optional(value)


def _check_endpoint_list(items: list[CardEndpointIn]) -> list[CardEndpointIn]:
    """整批校验：条数上限 + 名称不许重名。

    重名校验放在这里而不是单条模型上，因为"重复"是列表级别的性质。
    用 casefold 而不是 lower，是为了让中文/土耳其语之类的大小写也能正确归一。
    """
    if len(items) > MAX_CARD_ENDPOINTS:
        raise ValueError(f"一张卡片最多 {MAX_CARD_ENDPOINTS} 条环境地址")
    seen: set[str] = set()
    for item in items:
        key = item.name.casefold()
        if key in seen:
            raise ValueError(f"环境名称重复：{item.name}")
        seen.add(key)
    return items


class CardEndpointOut(ORMModel):
    id: int
    card_id: int
    slug: str
    name: str
    url: str
    open_mode: OpenMode
    verify_tls: bool | None = None
    sort_order: int
    # 供前端直接使用的跳转地址，由 service 计算（direct = url；proxy = /gw/{slug}/）
    launch_url: str = ""


class CardBase(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    icon: str = Field(default="", max_length=16)
    icon_style: str = Field(default="blue", max_length=16)
    icon_url: str = Field(default="", max_length=512)
    bg_style: str = Field(default="", max_length=24)
    accent_color: str = Field(default="", max_length=9)
    target_url: str = Field(min_length=1, max_length=1024)
    open_mode: OpenMode = OPEN_MODE_PROXY
    group_name: str = Field(default="默认分组", max_length=64)
    sort_order: int = Field(default=0, ge=-100000, le=100000)
    enabled: bool = True
    open_in_new_tab: bool = True
    verify_tls: bool | None = None

    @field_validator("title", "group_name", "icon")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("accent_color")
    @classmethod
    def _check_accent(cls, value: str) -> str:
        return _clean_accent(value)

    @field_validator("icon_url")
    @classmethod
    def _check_icon_url(cls, value: str) -> str:
        return _clean_icon_url(value)

    @field_validator("bg_style")
    @classmethod
    def _check_bg_style(cls, value: str) -> str:
        style = value.strip().lower()
        if style not in BG_STYLES:
            raise ValueError(f"卡片底色只能是 {BG_STYLES} 之一")
        return style

    @field_validator("target_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        url = value.strip()
        if not url.lower().startswith(_ALLOWED_TARGET_SCHEMES):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        return url


class CardCreate(CardBase):
    slug: str | None = Field(default=None, max_length=64)
    endpoints: list[CardEndpointIn] = Field(
        default_factory=list, max_length=MAX_CARD_ENDPOINTS
    )

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str | None) -> str | None:
        return _clean_slug_optional(value)

    @field_validator("endpoints")
    @classmethod
    def _check_endpoints(cls, value: list[CardEndpointIn]) -> list[CardEndpointIn]:
        return _check_endpoint_list(value)


class CardUpdate(BaseModel):
    """局部更新：所有字段可选。

    endpoints 的三态语义（service 用 exclude_unset 区分）：
      - 请求里没带这个键        → 环境地址原样不动
      - 带了 `"endpoints": []`  → 清空所有环境地址
      - 带了非空列表            → **整体替换**（按列表顺序重排）
    整体替换而不是逐条 diff，是因为前端编辑器本来就是"整份列表提交"，
    逐条 diff 只会让"改了名字"和"删了再加"长得一样，反而难以审计。
    """

    title: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    icon: str | None = Field(default=None, max_length=16)
    icon_style: str | None = Field(default=None, max_length=16)
    icon_url: str | None = Field(default=None, max_length=512)
    bg_style: str | None = Field(default=None, max_length=24)
    accent_color: str | None = Field(default=None, max_length=9)
    target_url: str | None = Field(default=None, min_length=1, max_length=1024)
    open_mode: OpenMode | None = None
    group_name: str | None = Field(default=None, max_length=64)
    sort_order: int | None = Field(default=None, ge=-100000, le=100000)
    enabled: bool | None = None
    open_in_new_tab: bool | None = None
    verify_tls: bool | None = None
    endpoints: list[CardEndpointIn] | None = Field(
        default=None, max_length=MAX_CARD_ENDPOINTS
    )

    @field_validator("endpoints")
    @classmethod
    def _check_endpoints(
        cls, value: list[CardEndpointIn] | None
    ) -> list[CardEndpointIn] | None:
        return None if value is None else _check_endpoint_list(value)

    @field_validator("accent_color")
    @classmethod
    def _check_accent(cls, value: str | None) -> str | None:
        return None if value is None else _clean_accent(value)

    @field_validator("icon_url")
    @classmethod
    def _check_icon_url(cls, value: str | None) -> str | None:
        return None if value is None else _clean_icon_url(value)

    @field_validator("bg_style")
    @classmethod
    def _check_bg_style(cls, value: str | None) -> str | None:
        if value is None:
            return None
        style = value.strip().lower()
        if style not in BG_STYLES:
            raise ValueError(f"卡片底色只能是 {BG_STYLES} 之一")
        return style

    @field_validator("target_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = value.strip()
        if not url.lower().startswith(_ALLOWED_TARGET_SCHEMES):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        return url


class CardOut(ORMModel):
    id: int
    slug: str
    title: str
    description: str
    icon: str
    icon_style: str
    icon_url: str = ""
    bg_style: str = ""
    accent_color: str = ""
    target_url: str
    open_mode: OpenMode
    group_name: str
    sort_order: int
    enabled: bool
    open_in_new_tab: bool
    click_count: int
    verify_tls: bool | None = None
    created_at: datetime
    updated_at: datetime
    # 卡片自己那条"默认地址"的跳转地址，由 service 计算。
    # 卡片挂了环境地址时前端优先弹选择框，这个字段仍然照常给出，
    # 以便列表页在没有环境地址时零分支地直接打开。
    launch_url: str = ""
    # 该卡片下的环境地址（按 sort_order 排好序）
    endpoints: list[CardEndpointOut] = Field(default_factory=list)


class CardReorderItem(BaseModel):
    id: int
    sort_order: int = Field(ge=-100000, le=100000)


class CardReorderRequest(BaseModel):
    items: list[CardReorderItem] = Field(min_length=1, max_length=500)


class CardGroupOut(BaseModel):
    name: str
    count: int


# ----------------------------------------------------------------------
# 图标上传
# ----------------------------------------------------------------------
class IconUploadOut(BaseModel):
    """上传成功后返回可直接写进 card.icon_url 的地址。"""

    url: str
    filename: str
    size: int
    content_type: str


# ----------------------------------------------------------------------
# 卡片配置导入 / 导出
# ----------------------------------------------------------------------
class CardExportItem(CardBase):
    """导出条目：只含"可迁移"的配置，不含 id / 点击量 / 时间戳。

    endpoints 里带着各自的 slug，导入时会尽量沿用（见 CardEndpointIn 的说明）。
    """

    model_config = ConfigDict(from_attributes=True)

    slug: str = Field(min_length=1, max_length=64)
    endpoints: list[CardEndpointIn] = Field(
        default_factory=list, max_length=MAX_CARD_ENDPOINTS
    )

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        return _clean_slug_required(value)

    @field_validator("endpoints")
    @classmethod
    def _check_endpoints(cls, value: list[CardEndpointIn]) -> list[CardEndpointIn]:
        return _check_endpoint_list(value)


class CardExportDoc(BaseModel):
    """导出文件顶层结构。version 用于将来兼容老文件。"""

    version: int = 1
    app: str = "dev-toolbox"
    exported_at: datetime
    count: int
    cards: list[CardExportItem]


class CardImportRequest(BaseModel):
    """导入请求。

    `cards` 故意用 `list[Any]` 而不是 `list[CardExportItem]`：
    批量导入时单条数据格式错误不应该让整批 422 失败，而是逐条校验、
    把失败原因收集到 `errors` 里返回，用户才能看到"哪几条没进来、为什么"。

    这里也不能写成 `list[dict]` —— Pydantic 会逐元素强校验，只要文件里混进
    一个非对象条目，整批就变成一句笼统的 422，与"逐条校验"的设计相悖。
    `CardService.import_cards` 里已经处理了"条目不是 JSON 对象"这种情况。
    """

    mode: Literal["merge", "replace"] = "merge"
    dry_run: bool = False
    cards: list[Any] = Field(min_length=1, max_length=5000)


class CardImportResult(BaseModel):
    mode: str
    dry_run: bool = False
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    total: int = 0
    errors: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------
# 用户管理
# ----------------------------------------------------------------------
class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default="", max_length=64)
    role: Role = "user"

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        username = value.strip()
        if not re.match(r"^[A-Za-z0-9_.\-]+$", username):
            raise ValueError("用户名只能包含字母、数字、下划线、点和连字符")
        return username


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    role: Role | None = None
    is_active: bool | None = None


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


# ----------------------------------------------------------------------
# 审计日志
# ----------------------------------------------------------------------
class AuditLogOut(ORMModel):
    id: int
    username: str
    action: str
    target_type: str
    target_id: str
    detail: str
    ip: str
    created_at: datetime


__all__ = [
    "BG_STYLES",
    "MAX_CARD_ENDPOINTS",
    "MessageOut",
    "LoginRequest",
    "UserOut",
    "SessionOut",
    "CardEndpointIn",
    "CardEndpointOut",
    "CardBase",
    "CardCreate",
    "CardUpdate",
    "CardOut",
    "CardReorderRequest",
    "CardReorderItem",
    "CardGroupOut",
    "IconUploadOut",
    "CardExportItem",
    "CardExportDoc",
    "CardImportRequest",
    "CardImportResult",
    "UserCreate",
    "UserUpdate",
    "PasswordChangeRequest",
    "AuditLogOut",
    "OPEN_MODE_DIRECT",
    "OPEN_MODE_PROXY",
]
