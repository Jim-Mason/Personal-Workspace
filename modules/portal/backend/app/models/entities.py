"""SQLAlchemy 实体定义（映射到迁移脚本建立的表结构）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """统一使用无时区的 UTC，避免 SQLite 存储时区信息丢失造成比较错误。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


# 卡片打开方式
OPEN_MODE_DIRECT = "direct"  # 浏览器直接跳转目标地址（需要本地本身可达）
OPEN_MODE_PROXY = "proxy"    # 经跳板机反向代理转发（本地无需可达）


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")  # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    icon_style: Mapped[str] = mapped_column(String(16), nullable=False, default="blue")
    # 自定义外观：图片图标（/uploads/... 或外链）、卡片底色预设、图标主色 #RRGGBB
    # 均为空字符串表示"用默认样式"
    icon_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    bg_style: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    accent_color: Mapped[str] = mapped_column(String(9), nullable=False, default="")
    target_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    open_mode: Mapped[str] = mapped_column(String(16), nullable=False, default=OPEN_MODE_PROXY)
    group_name: Mapped[str] = mapped_column(String(64), nullable=False, default="默认分组")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    open_in_new_tab: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 代理时是否按内网自签证书处理（默认关闭 TLS 校验，见 settings.proxy_verify_tls）
    verify_tls: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    # 多环境地址。lazy="selectin" 是刻意选的：列表接口会一次性带出所有卡片的地址，
    # 用默认的 lazy="select" 会退化成 N+1（每张卡片一条查询）。
    endpoints: Mapped[list["CardEndpoint"]] = relationship(
        "CardEndpoint",
        back_populates="card",
        cascade="all, delete-orphan",
        order_by="CardEndpoint.sort_order, CardEndpoint.id",
        lazy="selectin",
    )


class CardEndpoint(Base):
    """卡片下的一条环境地址（dev / test / prod …）。

    `slug` 是它在网关里的入口标识（`/gw/{slug}/`），与 `cards.slug` **共享同一个命名空间**：
    网关解析目标时先查卡片、再查环境地址，所以两者绝不能重名。
    全局唯一性由 `CardService` 保证 —— SQLite 不支持跨表唯一约束。

    每条地址自带 `open_mode`，因此同一个卡片可以「prod 走跳板机代理、dev/test 直连」。
    """

    __tablename__ = "card_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    open_mode: Mapped[str] = mapped_column(String(16), nullable=False, default=OPEN_MODE_DIRECT)
    verify_tls: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    card: Mapped["Card"] = relationship("Card", back_populates="endpoints")

    @property
    def target_url(self) -> str:
        """`url` 的别名。

        网关的转发逻辑、`build_launch_url` 只依赖 `slug` / `open_mode` /
        `target_url` 三个属性；给环境地址起一个同名只读别名，就能让同一段
        代码原样作用于"卡片"和"环境地址"两种对象，不必到处写 if isinstance。
        """
        return self.url


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user: Mapped[User | None] = relationship("User", lazy="joined")


__all__ = [
    "Base",
    "User",
    "Card",
    "CardEndpoint",
    "AuditLog",
    "utcnow",
    "new_id",
    "OPEN_MODE_DIRECT",
    "OPEN_MODE_PROXY",
]
