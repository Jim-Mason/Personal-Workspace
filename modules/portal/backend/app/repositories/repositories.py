"""仓储层：只负责数据访问，不含业务规则。

会话由调用方（服务层）注入，仓储自身不 commit —— 事务边界统一由服务层控制。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import AuditLog, Card, CardEndpoint, User, utcnow


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    def get_by_uid(self, uid: str) -> User | None:
        return self.db.scalar(select(User).where(User.uid == uid))

    def get_by_username(self, username: str) -> User | None:
        return self.db.scalar(select(User).where(User.username == username))

    def list_all(self) -> list[User]:
        return list(self.db.scalars(select(User).order_by(User.id.asc())))

    def count(self) -> int:
        return int(self.db.scalar(select(func.count()).select_from(User)) or 0)

    def count_active_admins(self, *, exclude_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(User).where(
            User.role == "admin", User.is_active.is_(True)
        )
        if exclude_id is not None:
            stmt = stmt.where(User.id != exclude_id)
        return int(self.db.scalar(stmt) or 0)

    def add(self, user: User) -> User:
        self.db.add(user)
        self.db.flush()
        return user

    def delete(self, user: User) -> None:
        self.db.delete(user)
        self.db.flush()

    def touch_login(self, user: User, when: datetime | None = None) -> None:
        user.last_login_at = when or utcnow()
        self.db.flush()


class CardRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, card_id: int) -> Card | None:
        return self.db.get(Card, card_id)

    def get_by_slug(self, slug: str) -> Card | None:
        return self.db.scalar(select(Card).where(Card.slug == slug))

    def slug_exists(self, slug: str) -> bool:
        return self.db.scalar(select(func.count()).select_from(Card).where(Card.slug == slug)) or 0 > 0

    def list_cards(
        self,
        *,
        enabled_only: bool = True,
        keyword: str | None = None,
        group_name: str | None = None,
    ) -> list[Card]:
        stmt = select(Card)
        if enabled_only:
            stmt = stmt.where(Card.enabled.is_(True))
        if keyword:
            pattern = f"%{keyword.strip()}%"
            stmt = stmt.where(
                Card.title.like(pattern)
                | Card.description.like(pattern)
                | Card.slug.like(pattern)
                | Card.group_name.like(pattern)
            )
        if group_name:
            stmt = stmt.where(Card.group_name == group_name)
        stmt = stmt.order_by(Card.sort_order.asc(), Card.id.asc())
        return list(self.db.scalars(stmt))

    def list_groups(self, *, enabled_only: bool = True) -> list[tuple[str, int]]:
        stmt = select(Card.group_name, func.count()).group_by(Card.group_name)
        if enabled_only:
            stmt = stmt.where(Card.enabled.is_(True))
        stmt = stmt.order_by(func.min(Card.sort_order).asc(), Card.group_name.asc())
        return [(str(row[0]), int(row[1])) for row in self.db.execute(stmt).all()]

    def max_sort_order(self) -> int:
        return int(self.db.scalar(select(func.coalesce(func.max(Card.sort_order), 0))) or 0)

    def add(self, card: Card) -> Card:
        self.db.add(card)
        self.db.flush()
        return card

    def delete(self, card: Card) -> None:
        self.db.delete(card)
        self.db.flush()

    def increment_click(self, card_id: int) -> None:
        card = self.db.get(Card, card_id)
        if card is not None:
            card.click_count = (card.click_count or 0) + 1
            self.db.flush()


class CardEndpointRepository:
    """卡片的环境地址。

    网关按 slug 解析目标时会用它 —— 环境地址的 slug 与卡片的 slug
    共享同一个入口命名空间，所以 `slug_taken` 必须两张表一起查。
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, endpoint_id: int) -> CardEndpoint | None:
        return self.db.get(CardEndpoint, endpoint_id)

    def list_for_card(self, card_id: int) -> list[CardEndpoint]:
        stmt = (
            select(CardEndpoint)
            .where(CardEndpoint.card_id == card_id)
            .order_by(CardEndpoint.sort_order.asc(), CardEndpoint.id.asc())
        )
        return list(self.db.scalars(stmt))

    def get_by_slug(self, slug: str) -> CardEndpoint | None:
        return self.db.scalar(select(CardEndpoint).where(CardEndpoint.slug == slug))

    def slug_taken(self, slug: str) -> bool:
        """入口标识在 cards 与 card_endpoints 两个命名空间里都必须唯一。"""
        in_cards = self.db.scalar(
            select(func.count()).select_from(Card).where(Card.slug == slug)
        )
        if in_cards:
            return True
        in_endpoints = self.db.scalar(
            select(func.count()).select_from(CardEndpoint).where(CardEndpoint.slug == slug)
        )
        return bool(in_endpoints)

    def add(self, endpoint: CardEndpoint) -> CardEndpoint:
        self.db.add(endpoint)
        self.db.flush()
        return endpoint

    def delete(self, endpoint: CardEndpoint) -> None:
        self.db.delete(endpoint)
        self.db.flush()


class AuditRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, log: AuditLog) -> AuditLog:
        self.db.add(log)
        self.db.flush()
        return log

    def list_recent(self, *, limit: int = 100, offset: int = 0) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .order_by(AuditLog.id.desc())
            .limit(min(limit, 500))
            .offset(max(offset, 0))
        )
        return list(self.db.scalars(stmt))


__all__ = ["UserRepository", "CardRepository", "CardEndpointRepository", "AuditRepository"]
