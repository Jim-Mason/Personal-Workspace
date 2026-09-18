"""卡片图标上传与访问。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import FileResponse

from app.api.deps import AdminUser, ClientIp, CurrentUser, DbSession
from app.core.config import settings
from app.core.errors import BadRequestError, ForbiddenError, NotFoundError
from app.models.entities import AuditLog
from app.repositories.repositories import AuditRepository
from app.schemas.models import IconUploadOut
from app.services.upload_service import UploadService

logger = logging.getLogger("app.api.uploads")

router = APIRouter(prefix="/uploads", tags=["uploads"])

# 图标的静态访问挂在根路径（不是 /api 下），由 main.py 直接注册
assets_router = APIRouter(tags=["uploads"])


@router.post(
    "/icon",
    response_model=IconUploadOut,
    status_code=status.HTTP_201_CREATED,
    summary="上传卡片图标（管理员）",
)
async def upload_icon(
    db: DbSession,
    admin: AdminUser,
    ip: ClientIp,
    file: UploadFile = File(..., description="PNG / JPG / WEBP / GIF / ICO / BMP，≤512KB"),
) -> IconUploadOut:
    """上传后返回 `/uploads/icons/<uuid>.<ext>`，直接写进卡片的 icon_url 即可。

    服务端只按**文件头**判定类型，不信任 filename 与 content-type；
    SVG 一律拒绝（XML 可内嵌脚本，直接访问该 URL 会形成同源 XSS）。
    """
    if not settings.uploads_enabled:
        raise ForbiddenError("图标上传功能已关闭")

    stored = await UploadService().save_icon(file)

    AuditRepository(db).add(
        AuditLog(
            username=admin.username,
            action="icon.upload",
            target_type="upload",
            target_id=stored.filename,
            detail=f"{stored.size} bytes -> {stored.url}",
            ip=ip,
        )
    )
    db.commit()

    return IconUploadOut(
        url=stored.url,
        filename=stored.filename,
        size=stored.size,
        content_type=stored.content_type,
    )


@assets_router.get("/uploads/icons/{filename}", summary="读取卡片图标")
async def read_icon(filename: str, user: CurrentUser) -> FileResponse:
    """图标读取需要登录。

    没有用 StaticFiles 挂载，是因为静态挂载无法叠加鉴权依赖 ——
    那会让上传的图标成为公网可读资源，与"门户所有页面都要登录"的预期不一致。
    文件很小（≤512KB），走 FileResponse 的性能差异可以忽略。
    """
    try:
        path = UploadService.resolve_icon(filename)
    except BadRequestError as exc:
        raise NotFoundError("图标不存在") from exc
    if not path.is_file():
        raise NotFoundError("图标不存在")

    # CSP / nosniff / 长缓存等响应头由 main.py 的中间件按路径统一加
    return FileResponse(path)
