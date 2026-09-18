"""认证与用户管理业务逻辑。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, UnauthorizedError
from app.core.security import hash_password, needs_rehash, verify_password
from app.models.entities import AuditLog, User, utcnow
from app.repositories.repositories import AuditRepository, UserRepository
from app.schemas.models import UserCreate, UserUpdate

logger = logging.getLogger("app.service.auth")

# 防止暴力破解：同一用户名连续失败达到阈值后短暂锁定
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300


class AuthService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.users = UserRepository(db)
        self.audit = AuditRepository(db)
        self._failures: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------
    def _locked_for(self, username: str) -> int:
        """返回剩余锁定秒数，0 表示未锁定。"""
        record = self._failures.get(username)
        if not record:
            return 0
        count, last = record
        elapsed = utcnow().timestamp() - last
        if elapsed >= _LOCKOUT_SECONDS:
            self._failures.pop(username, None)
            return 0
        if count < _MAX_FAILED_ATTEMPTS:
            return 0
        return int(_LOCKOUT_SECONDS - elapsed)

    def _register_failure(self, username: str) -> None:
        count, _ = self._failures.get(username, (0, 0.0))
        self._failures[username] = (count + 1, utcnow().timestamp())

    def authenticate(self, username: str, password: str, *, ip: str = "") -> User:
        remaining = self._locked_for(username)
        if remaining:
            raise UnauthorizedError(
                f"登录失败次数过多，请 {remaining} 秒后再试", code="too_many_attempts", http_status=429
            )

        user = self.users.get_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            self._register_failure(username)
            self.audit.add(
                AuditLog(
                    username=username,
                    action="auth.login_failed",
                    target_type="user",
                    target_id=username,
                    detail="用户名或密码错误",
                    ip=ip,
                )
            )
            self.db.commit()
            raise UnauthorizedError("用户名或密码错误")

        if not user.is_active:
            raise ForbiddenError("该账号已被禁用，请联系管理员", code="user_disabled")

        self._failures.pop(username, None)

        # 参数升级后自动重哈希
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
            logger.info("已为用户 %s 升级密码哈希参数", user.username)

        self.users.touch_login(user)
        self.audit.add(
            AuditLog(
                username=user.username,
                action="auth.login",
                target_type="user",
                target_id=user.uid,
                detail="登录成功",
                ip=ip,
            )
        )
        self.db.commit()
        return user

    def change_password(self, user: User, old_password: str, new_password: str, *, ip: str = "") -> None:
        if not verify_password(old_password, user.password_hash):
            raise BadRequestError("原密码不正确", code="invalid_old_password")
        if verify_password(new_password, user.password_hash):
            raise BadRequestError("新密码不能与原密码相同", code="password_reused")
        user.password_hash = hash_password(new_password)
        self.audit.add(
            AuditLog(
                username=user.username,
                action="user.change_password",
                target_type="user",
                target_id=user.uid,
                detail="修改本人密码",
                ip=ip,
            )
        )
        self.db.commit()


class UserService:
    """管理员的用户维护能力。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.users = UserRepository(db)
        self.audit = AuditRepository(db)

    def list_users(self) -> list[User]:
        return self.users.list_all()

    def create(self, payload: UserCreate, *, actor: str, ip: str = "") -> User:
        if self.users.get_by_username(payload.username) is not None:
            raise ConflictError(f"用户名 {payload.username} 已存在", code="username_taken")
        user = User(
            username=payload.username,
            display_name=payload.display_name or payload.username,
            password_hash=hash_password(payload.password),
            role=payload.role,
            is_active=True,
        )
        self.users.add(user)
        self.audit.add(
            AuditLog(
                username=actor,
                action="user.create",
                target_type="user",
                target_id=payload.username,
                detail=f"角色={payload.role}",
                ip=ip,
            )
        )
        self.db.commit()
        return user

    def update(self, uid: str, payload: UserUpdate, *, actor: str, ip: str = "") -> User:
        user = self.users.get_by_uid(uid)
        if user is None:
            raise BadRequestError("用户不存在", code="user_not_found", http_status=404)

        changes = payload.model_dump(exclude_unset=True)

        # 不允许把最后一个可用管理员降级或禁用，否则系统会失去管理入口
        demoting = changes.get("role") == "user" and user.role == "admin"
        disabling = changes.get("is_active") is False and user.is_active and user.role == "admin"
        if (demoting or disabling) and self.users.count_active_admins(exclude_id=user.id) == 0:
            raise BadRequestError(
                "系统必须保留至少一个启用状态的管理员", code="last_admin_protected"
            )

        if "password" in changes:
            user.password_hash = hash_password(changes.pop("password"))
        for field, value in changes.items():
            setattr(user, field, value)

        self.audit.add(
            AuditLog(
                username=actor,
                action="user.update",
                target_type="user",
                target_id=user.username,
                detail=",".join(sorted(changes.keys())) or "无字段变更",
                ip=ip,
            )
        )
        self.db.commit()
        return user

    def delete(self, uid: str, *, actor: str, current_user_id: int, ip: str = "") -> None:
        user = self.users.get_by_uid(uid)
        if user is None:
            raise BadRequestError("用户不存在", code="user_not_found", http_status=404)
        if user.id == current_user_id:
            raise BadRequestError("不能删除当前登录的账号", code="cannot_delete_self")
        if user.role == "admin" and user.is_active:
            if self.users.count_active_admins(exclude_id=user.id) == 0:
                raise BadRequestError(
                    "系统必须保留至少一个启用状态的管理员", code="last_admin_protected"
                )
        username = user.username
        self.users.delete(user)
        self.audit.add(
            AuditLog(
                username=actor,
                action="user.delete",
                target_type="user",
                target_id=username,
                detail=f"删除用户 {username}",
                ip=ip,
            )
        )
        self.db.commit()


__all__ = ["AuthService", "UserService"]
