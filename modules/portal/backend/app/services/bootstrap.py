"""启动引导：建表、跑迁移、创建初始管理员。

设计原则：全部幂等，重复启动不会产生副作用。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password
from app.db.migrate import upgrade
from app.db.session import SessionLocal
from app.models.entities import User, utcnow

logger = logging.getLogger("app.bootstrap")


def run_migrations() -> None:
    applied = upgrade()
    if applied:
        logger.info("已应用数据库迁移: %s", applied)
    else:
        logger.info("数据库结构已是最新")


def ensure_admin() -> None:
    if not settings.allow_bootstrap_admin:
        return

    with SessionLocal() as db:
        existing = db.scalar(select(User).limit(1))
        if existing is not None:
            return

        password = settings.bootstrap_admin_password
        generated = False
        if not password:
            import secrets

            password = secrets.token_urlsafe(12)
            generated = True

        admin = User(
            username=settings.bootstrap_admin_username,
            display_name="系统管理员",
            password_hash=hash_password(password),
            role="admin",
            is_active=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(admin)
        db.commit()

        if generated:
            banner = (
                "\n"
                + "=" * 68
                + "\n  已创建初始管理员账号（请立即登录后修改密码）\n"
                + f"    用户名: {settings.bootstrap_admin_username}\n"
                + f"    密  码: {password}\n"
                + "  该密码仅在本次输出，之后无法再次查看。\n"
                + "=" * 68
            )
            # 用 print 而不是 logger：这是需要人工抄录的一次性信息
            print(banner, flush=True)
            logger.warning("已生成随机管理员密码，请从启动日志中抄录后立即修改")
        else:
            logger.info("已按 BOOTSTRAP_ADMIN_PASSWORD 创建初始管理员 %s", admin.username)


def bootstrap() -> None:
    run_migrations()
    ensure_admin()


__all__ = ["bootstrap", "run_migrations", "ensure_admin"]
