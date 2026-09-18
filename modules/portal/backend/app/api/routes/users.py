"""用户管理端点（仅管理员）。

路径使用 uid（随机标识）而非自增主键，避免暴露用户规模、也防止被枚举。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AdminUser, ClientIp, DbSession
from app.models.entities import User
from app.schemas.models import MessageOut, UserCreate, UserOut, UserUpdate
from app.services.auth_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


def _to_out(user: User) -> UserOut:
    return UserOut.model_validate(user)


@router.get("", response_model=list[UserOut], summary="用户列表")
async def list_users(db: DbSession, admin: AdminUser) -> list[UserOut]:
    return [_to_out(user) for user in UserService(db).list_users()]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="新建用户")
async def create_user(
    payload: UserCreate, db: DbSession, admin: AdminUser, ip: ClientIp
) -> UserOut:
    return _to_out(UserService(db).create(payload, actor=admin.username, ip=ip))


@router.patch("/{uid}", response_model=UserOut, summary="更新用户")
async def update_user(
    uid: str, payload: UserUpdate, db: DbSession, admin: AdminUser, ip: ClientIp
) -> UserOut:
    return _to_out(UserService(db).update(uid, payload, actor=admin.username, ip=ip))


@router.delete("/{uid}", response_model=MessageOut, summary="删除用户")
async def delete_user(uid: str, db: DbSession, admin: AdminUser, ip: ClientIp) -> MessageOut:
    UserService(db).delete(uid, actor=admin.username, current_user_id=admin.id, ip=ip)
    return MessageOut(message="用户已删除")
