"""卡片 CRUD、排序、连通性自检、配置导入导出。"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from fastapi.responses import Response

from app.api.deps import AdminUser, ClientIp, CurrentUser, DbSession
from app.core.config import settings
from app.core.errors import AppError, BadRequestError, NotFoundError
from app.schemas.models import (
    CardCreate,
    CardGroupOut,
    CardImportRequest,
    CardImportResult,
    CardOut,
    CardReorderRequest,
    CardUpdate,
    MessageOut,
)
from app.services.card_service import CardService
from app.services.card_sheet import parse_sheet, to_csv, to_json, to_xlsx
from app.services.proxy_guard import validate_target

logger = logging.getLogger("app.api.cards")

router = APIRouter(prefix="/cards", tags=["cards"])

# 导出格式 → (Content-Type, 文件扩展名)
_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "json": ("application/json; charset=utf-8", "json"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
}


@router.get("", response_model=list[CardOut], summary="卡片列表")
async def list_cards(
    db: DbSession,
    user: CurrentUser,
    keyword: str | None = Query(default=None, max_length=128, description="按标题/描述/slug 模糊搜索"),
    group_name: str | None = Query(default=None, max_length=64, description="按分组过滤"),
    include_disabled: bool = Query(default=False, description="是否包含已停用卡片（仅管理员生效）"),
) -> list[CardOut]:
    only_enabled = not (include_disabled and user.is_admin)
    return CardService(db).list_cards(
        enabled_only=only_enabled, keyword=keyword, group_name=group_name
    )


@router.get("/groups", response_model=list[CardGroupOut], summary="分组及其卡片数量")
async def list_groups(db: DbSession, user: CurrentUser) -> list[CardGroupOut]:
    groups = CardService(db).list_groups(enabled_only=not user.is_admin)
    return [CardGroupOut(**item) for item in groups]


@router.post("", response_model=CardOut, status_code=status.HTTP_201_CREATED, summary="新建卡片（管理员）")
async def create_card(
    payload: CardCreate, db: DbSession, admin: AdminUser, ip: ClientIp
) -> CardOut:
    return CardService(db).create(payload, actor=admin.username, ip=ip)


@router.post("/reorder", response_model=MessageOut, summary="批量调整排序（管理员）")
async def reorder_cards(
    payload: CardReorderRequest, db: DbSession, admin: AdminUser, ip: ClientIp
) -> MessageOut:
    updated = CardService(db).reorder(
        [(item.id, item.sort_order) for item in payload.items], actor=admin.username, ip=ip
    )
    return MessageOut(message=f"已更新 {updated} 张卡片的排序")


# ----------------------------------------------------------------------
# 导入导出
# 注意：这几个路径必须注册在 `/{card_id}` 之前，否则会被路径参数吞掉
# ----------------------------------------------------------------------
@router.get("/export", summary="导出全部卡片配置（管理员）")
async def export_cards(
    db: DbSession,
    admin: AdminUser,
    fmt: Literal["json", "xlsx", "csv"] = Query(
        default="json",
        alias="format",
        description="导出格式：json（默认，含 version/exported_at）/ xlsx（Excel 表格）/ csv",
    ),
) -> Response:
    """导出全部卡片为可下载文件。

    - `json`：完整结构，供脚本处理与换机备份，也是历史默认格式
    - `xlsx`：带字段说明页、枚举下拉与表头批注，适合在 Excel 里成批修改
    - `csv`：纯文本表格，带 UTF-8 BOM，Excel 双击打开不乱码

    三种格式的列定义都来自 `card_sheet.CARD_COLUMNS`，导入时按同一份定义解析，
    所以"导出 → 在 Excel 里改 → 再导入"是闭合的。
    """
    doc = CardService(db).export_cards()
    media_type, extension = _EXPORT_FORMATS[fmt]

    if fmt == "xlsx":
        body = to_xlsx(doc)
    elif fmt == "csv":
        body = to_csv(doc)
    else:
        body = to_json(doc)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"dev-toolbox-cards-{stamp}.{extension}"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            # 让前端不必解析响应体就能提示"已导出 N 张"
            "X-Card-Count": str(doc.count),
        },
    )


@router.post("/import", response_model=CardImportResult, summary="导入卡片配置（管理员）")
async def import_cards(
    payload: CardImportRequest, db: DbSession, admin: AdminUser, ip: ClientIp
) -> CardImportResult:
    """批量导入（JSON 请求体）。单条数据非法只跳过该条，结果里会说明失败原因。"""
    return CardService(db).import_cards(payload, actor=admin.username, ip=ip)


@router.post(
    "/import/file",
    response_model=CardImportResult,
    summary="从表格文件导入卡片配置（管理员）",
)
async def import_cards_file(
    db: DbSession,
    admin: AdminUser,
    ip: ClientIp,
    file: UploadFile = File(..., description=".xlsx / .xlsm / .csv / .json，≤5MB"),
    mode: Literal["merge", "replace"] = Form(default="merge", description="merge=合并，replace=覆盖全部"),
    dry_run: bool = Form(default=False, description="true=只预演并回滚"),
) -> CardImportResult:
    """上传 xlsx / csv / json 导入。

    与 `POST /cards/import` 共用同一个 `CardService.import_cards`，
    这个入口只多做一步「文件字节 → 记录列表」的解码（`card_sheet.parse_sheet`），
    因此两条路径的校验规则、覆盖语义、错误文案完全一致。

    表格解析通过 `row_labels` / `extra_errors` 把错误定位到**文件里的物理行号**，
    用户拿着 "第 7 行" 就能直接在 Excel 里找到那一条。
    """
    limit = settings.upload_max_import_bytes
    blob = await file.read(limit + 1)
    if len(blob) > limit:
        raise BadRequestError(f"导入文件不能超过 {limit // (1024 * 1024)} MB")

    sheet = parse_sheet(blob, file.filename or "")

    if not sheet.cards:
        if sheet.total == 0:
            raise BadRequestError("表格里只有表头，没有可导入的数据行。")
        # 所有数据行都不合法：返回"全部跳过"的结果，让用户逐条看清原因，
        # 比抛一个笼统的 400"没有数据行"有用得多
        return CardImportResult(
            mode=mode,
            dry_run=dry_run,
            skipped=sheet.total,
            total=sheet.total,
            errors=list(sheet.errors),
        )

    payload = CardImportRequest(mode=mode, dry_run=dry_run, cards=sheet.cards)
    result = CardService(db).import_cards(
        payload,
        actor=admin.username,
        ip=ip,
        row_labels=sheet.row_numbers,
        extra_errors=sheet.errors,
        total=sheet.total,
    )
    logger.info(
        "文件导入 %s：解析 %d 行、可用 %d 行、跳过 %d 行",
        file.filename,
        sheet.total,
        len(sheet.cards),
        result.skipped,
    )
    return result


@router.get("/{card_id}", response_model=CardOut, summary="卡片详情")
async def get_card(card_id: int, db: DbSession, user: CurrentUser) -> CardOut:
    card = CardService(db).get(card_id)
    if not card.enabled and not user.is_admin:
        raise NotFoundError("卡片不存在")
    return card


@router.patch("/{card_id}", response_model=CardOut, summary="更新卡片（管理员）")
async def update_card(
    card_id: int, payload: CardUpdate, db: DbSession, admin: AdminUser, ip: ClientIp
) -> CardOut:
    return CardService(db).update(card_id, payload, actor=admin.username, ip=ip)


@router.delete("/{card_id}", response_model=MessageOut, summary="删除卡片（管理员）")
async def delete_card(card_id: int, db: DbSession, admin: AdminUser, ip: ClientIp) -> MessageOut:
    CardService(db).delete(card_id, actor=admin.username, ip=ip)
    return MessageOut(message="卡片已删除")


@router.post("/{card_id}/click", response_model=MessageOut, summary="记录一次打开（直连模式使用）")
async def record_click(
    card_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    endpoint: str | None = Query(
        default=None, max_length=80, description="点开的是哪条环境地址（标识），点卡片本体时留空"
    ),
) -> MessageOut:
    service = CardService(db)
    card = service.get_entity(card_id)
    if not card.enabled:
        raise NotFoundError("卡片不存在或已停用")
    service.record_click(card, actor=user.username, ip=ip, endpoint_slug=endpoint or "")
    return MessageOut(message="ok")


@router.post("/{card_id}/check", summary="连通性自检：从跳板机探测目标是否可达（管理员）")
async def check_card(
    card_id: int,
    db: DbSession,
    admin: AdminUser,
    endpoint: str | None = Query(
        default=None, max_length=80, description="只探测某条环境地址（标识），留空则探测卡片本体的地址"
    ),
) -> dict[str, object]:
    """在跳板机本地发起一次探测，帮助快速判断"是配置问题还是网络问题"。

    带 `endpoint` 时探测那条环境地址，并按它**自己的** verify_tls 决定是否校验证书
    —— 同一张卡片里 prod 和 dev 的证书情况可能完全不同。
    """
    import httpx

    from app.repositories.repositories import CardEndpointRepository

    card = CardService(db).get_entity(card_id)
    target_url = card.target_url
    verify_tls_setting = card.verify_tls
    label = card.slug

    if endpoint:
        record = CardEndpointRepository(db).get_by_slug(endpoint)
        if record is None or record.card_id != card.id:
            raise NotFoundError("环境地址不存在")
        target_url = record.url
        verify_tls_setting = record.verify_tls
        label = f"{card.slug}/{record.slug}"

    try:
        info = validate_target(target_url)
    except AppError as exc:
        return {"reachable": False, "stage": "validate", "message": exc.message, "target": label}

    verify_tls = settings.proxy_verify_tls if verify_tls_setting is None else bool(verify_tls_setting)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            verify=verify_tls,
            follow_redirects=False,
            timeout=httpx.Timeout(settings.proxy_timeout_seconds, connect=settings.proxy_connect_timeout_seconds),
            trust_env=settings.proxy_trust_env,
        ) as client:
            response = await client.request("HEAD", info.url)
        elapsed = int((time.perf_counter() - started) * 1000)
        return {
            "reachable": True,
            "stage": "connect",
            "target": label,
            "status_code": response.status_code,
            "latency_ms": elapsed,
            "server": response.headers.get("server", ""),
            "content_type": response.headers.get("content-type", ""),
            "message": f"目标可达，HTTP {response.status_code}，耗时 {elapsed} ms",
        }
    except httpx.ConnectError as exc:
        return {
            "reachable": False,
            "stage": "connect",
            "target": label,
            "message": f"TCP 连接失败：{exc}",
            "hint": "多为端口未监听或被安全组/防火墙拦截",
        }
    except httpx.ConnectTimeout as exc:
        return {
            "reachable": False,
            "stage": "connect",
            "target": label,
            "message": f"连接超时：{exc}",
            "hint": "网络不通或 SYN 被丢弃",
        }
    except httpx.HTTPError as exc:
        return {
            "reachable": False,
            "stage": "request",
            "target": label,
            "message": f"{type(exc).__name__}: {exc}",
        }
