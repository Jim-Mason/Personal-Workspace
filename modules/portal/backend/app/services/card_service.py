"""卡片业务逻辑：负责 slug 生成、排序、launch_url 计算与审计。"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.entities import (
    OPEN_MODE_DIRECT,
    OPEN_MODE_PROXY,
    AuditLog,
    Card,
    CardEndpoint,
    utcnow,
)
from app.repositories.repositories import (
    AuditRepository,
    CardEndpointRepository,
    CardRepository,
)
from app.schemas.models import (
    CardCreate,
    CardEndpointIn,
    CardExportDoc,
    CardExportItem,
    CardImportRequest,
    CardImportResult,
    CardOut,
    CardUpdate,
)
from app.services.proxy_guard import validate_target

logger = logging.getLogger("app.service.card")

# 导入时可覆盖的字段（slug 是主键语义，单独处理）
_IMPORT_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "icon",
    "icon_style",
    "icon_url",
    "bg_style",
    "accent_color",
    "target_url",
    "open_mode",
    "group_name",
    "sort_order",
    "enabled",
    "open_in_new_tab",
    "verify_tls",
)

# 导入结果里最多返回多少条错误详情（避免 5000 条错误把响应撑爆）
_MAX_IMPORT_ERRORS = 50


def _slugify(value: str) -> str:
    """把中文/英文标题转成 URL 安全的 slug。中文会被转成拼音风格不可行，这里退化为 ascii 摘要。"""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return slug[:60]


def _brief(exc: ValidationError) -> str:
    """把 Pydantic 校验错误压成一行可读文案：`字段: 原因; 字段: 原因`。"""
    parts: list[str] = []
    for error in exc.errors()[:4]:
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "__root__")
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "格式不正确"


class CardService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.cards = CardRepository(db)
        self.endpoints = CardEndpointRepository(db)
        self.audit = AuditRepository(db)

    # ------------------------------------------------------------------
    def _unique_slug(self, base: str, *, exclude_id: int | None = None) -> str:
        """给卡片挑一个入口标识。

        必须同时避开 `cards.slug` 与 `card_endpoints.slug` —— 网关解析
        `/gw/{slug}/` 时先查卡片再查环境地址，两者撞名会让其中一方永远打不开。
        SQLite 不支持跨表唯一约束，这层保证只能写在服务里。
        """
        candidate = base or "card"
        suffix = 2
        while True:
            existing = self.cards.get_by_slug(candidate)
            card_ok = existing is None or (exclude_id is not None and existing.id == exclude_id)
            if card_ok and self.endpoints.get_by_slug(candidate) is None:
                return candidate
            candidate = f"{base}-{suffix}"
            suffix += 1

    def _unique_endpoint_slug(self, card: Card, item: CardEndpointIn, index: int) -> str:
        """给一条环境地址挑入口标识，形如 `jenkins-prod` / `jenkins-3`。

        为什么拼上卡片 slug：环境名基本都是「生产 / 测试 / 预发」这类中文，
        NFKD 转 ascii 后会变成空串，退化成 `env-1`、`env-2` 这种换了卡片就分不清
        的标识。拼上卡片 slug 之后，即便名字全是中文，地址标识依然能一眼认出归属。

        名字本身能转出 ascii（prod / test / uat / dev）时优先用名字，读起来最直观。
        """
        prefix = card.slug[:40].strip("-") or "card"
        name_part = _slugify(item.name)[:28].strip("-")
        base = f"{prefix}-{name_part}" if name_part else f"{prefix}-{index + 1}"

        candidate = base
        suffix = 2
        while self.endpoints.slug_taken(candidate):
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _sync_endpoints(self, card: Card, items: Sequence[CardEndpointIn]) -> None:
        """把卡片的环境地址同步成 `items`（列表顺序即展示顺序）。

        逐个按 slug 认领已存在的记录、只增删差异，而不是"全删再全建"：
        全删重建会让每条地址的 id 每次都变，也会让 updated_at 无谓地刷新，
        查问题时看不出到底改没改。前端把已存在地址的 slug 原样回传，
        这里就能认出来。

        `item.slug` 为空 = 新增（前端新加的一行），此时按卡片 slug + 环境名生成；
        若传了 slug 但库里没有（比如从别的机器导入），也走新增分支。
        """
        existing: dict[str, CardEndpoint] = {ep.slug: ep for ep in card.endpoints}
        kept: set[str] = set()

        for index, item in enumerate(items):
            # 每条地址按**自身**的 open_mode 校验：同卡片里 prod 走代理、dev 直连
            # 是允许的，所以不能拿卡片级的 open_mode 去套
            self._assert_target_acceptable(item.url, item.open_mode)

            order = (index + 1) * 10
            current = existing.get(item.slug) if item.slug else None
            if current is not None:
                kept.add(current.slug)
                current.name = item.name
                current.url = item.url
                current.open_mode = item.open_mode
                current.verify_tls = item.verify_tls
                current.sort_order = order
                continue

            endpoint = CardEndpoint(
                slug=self._unique_endpoint_slug(card, item, index),
                name=item.name,
                url=item.url,
                open_mode=item.open_mode,
                verify_tls=item.verify_tls,
                sort_order=order,
            )
            # 走关系而不是直接写 card_id：新建卡片时 id 还没生成，
            # 由 ORM 在 flush 时按父对象的 id 补齐外键
            card.endpoints.append(endpoint)
            kept.add(endpoint.slug)

        for slug, endpoint in existing.items():
            if slug not in kept:
                # cascade="all, delete-orphan" 会把移出集合的记录一并删掉
                card.endpoints.remove(endpoint)

        # 认领老记录时是按旧顺序就地改写的，新记录追加在末尾，
        # 内存里的集合顺序因此可能是乱的 —— 这里按 sort_order 重排一次，
        # 让紧随其后的 _to_out 拿到正确顺序（集合一旦重新从库里加载，
        # relationship 的 order_by 也会给出同样的顺序）
        card.endpoints.sort(key=lambda ep: (ep.sort_order, ep.id or 0))

    def _to_out(self, card: Card) -> CardOut:
        # 延迟导入避免 core <-> service 循环依赖
        from app.services.proxy_service import build_launch_url

        data = CardOut.model_validate(card)
        data.launch_url = build_launch_url(card)
        # 按 slug 而不是按下标配对：ORM 侧与 Pydantic 侧的排序虽然一致，
        # 但用 slug 配对不会因为将来某一侧改了排序规则而错位
        launch_by_slug = {
            endpoint.slug: build_launch_url(endpoint) for endpoint in card.endpoints
        }
        for endpoint in data.endpoints:
            endpoint.launch_url = launch_by_slug.get(endpoint.slug, "")
        return data

    # ------------------------------------------------------------------
    def list_cards(
        self,
        *,
        enabled_only: bool = True,
        keyword: str | None = None,
        group_name: str | None = None,
    ) -> list[CardOut]:
        cards = self.cards.list_cards(
            enabled_only=enabled_only, keyword=keyword, group_name=group_name
        )
        return [self._to_out(card) for card in cards]

    def list_groups(self, *, enabled_only: bool = True) -> list[dict[str, object]]:
        return [
            {"name": name, "count": count}
            for name, count in self.cards.list_groups(enabled_only=enabled_only)
        ]

    def get(self, card_id: int) -> CardOut:
        card = self.cards.get_by_id(card_id)
        if card is None:
            raise NotFoundError("卡片不存在")
        return self._to_out(card)

    def get_entity(self, card_id: int) -> Card:
        card = self.cards.get_by_id(card_id)
        if card is None:
            raise NotFoundError("卡片不存在")
        return card

    def get_enabled_by_slug(self, slug: str) -> Card:
        card = self.cards.get_by_slug(slug)
        if card is None or not card.enabled:
            raise NotFoundError(f"卡片 {slug} 不存在或已停用")
        return card

    # ------------------------------------------------------------------
    def _assert_target_acceptable(self, target_url: str, open_mode: str) -> None:
        """在保存配置时就拦住明显不合法的目标，避免用户点了卡片才看到报错。

        direct 模式下地址由浏览器直接打开、不经过跳板机，因此不做 SSRF 相关限制。
        """
        if open_mode != OPEN_MODE_PROXY:
            return
        # 保存阶段不做 DNS 可行性拦截（域名可能尚未就绪），但安全规则照常生效
        validate_target(target_url, strict_dns=False)

    def create(self, payload: CardCreate, *, actor: str, ip: str = "") -> CardOut:
        self._assert_target_acceptable(payload.target_url, payload.open_mode)

        base_slug = (payload.slug or _slugify(payload.title) or "card").lower()
        slug = self._unique_slug(base_slug)
        if slug != base_slug:
            logger.info("slug 已被占用，自动调整为 %s -> %s", base_slug, slug)

        sort_order = payload.sort_order
        if sort_order == 0:
            sort_order = self.cards.max_sort_order() + 10

        card = Card(
            slug=slug,
            title=payload.title,
            description=payload.description,
            icon=payload.icon,
            icon_style=payload.icon_style,
            icon_url=payload.icon_url,
            bg_style=payload.bg_style,
            accent_color=payload.accent_color,
            target_url=payload.target_url,
            open_mode=payload.open_mode,
            group_name=payload.group_name or "默认分组",
            sort_order=sort_order,
            enabled=payload.enabled,
            open_in_new_tab=payload.open_in_new_tab,
            verify_tls=payload.verify_tls,
        )
        self.cards.add(card)
        # 先 flush 拿到 card.id：_unique_endpoint_slug 要查库判断标识是否被占用，
        # 而查询会触发 autoflush；显式 flush 让顺序可控，也让审计日志能记到真实 id
        self.db.flush()
        if payload.endpoints:
            self._sync_endpoints(card, payload.endpoints)
        self._log(actor, "card.create", card, ip)
        self.db.commit()
        return self._to_out(card)

    def update(self, card_id: int, payload: CardUpdate, *, actor: str, ip: str = "") -> CardOut:
        card = self.get_entity(card_id)
        changes = payload.model_dump(exclude_unset=True)
        # endpoints 不能走下面的 setattr 循环：它是关系集合，要整体同步，
        # 直接赋值成 dict 列表只会把关系字段写坏
        endpoint_items = (
            payload.endpoints if "endpoints" in payload.model_fields_set else None
        )
        changes.pop("endpoints", None)
        if not changes and endpoint_items is None:
            return self._to_out(card)

        self._assert_target_acceptable(
            changes.get("target_url", card.target_url),
            changes.get("open_mode", card.open_mode),
        )

        for field, value in changes.items():
            setattr(card, field, value)

        if endpoint_items is not None:
            self._sync_endpoints(card, endpoint_items)

        detail_parts = sorted(changes)
        if endpoint_items is not None:
            detail_parts.append(f"endpoints={len(endpoint_items)}")
        self._log(actor, "card.update", card, ip, detail=",".join(detail_parts))
        self.db.commit()
        return self._to_out(card)

    def delete(self, card_id: int, *, actor: str, ip: str = "") -> None:
        card = self.get_entity(card_id)
        slug = card.slug
        self.cards.delete(card)
        self.audit.add(
            AuditLog(
                username=actor,
                action="card.delete",
                target_type="card",
                target_id=slug,
                detail=f"删除卡片 {slug}",
                ip=ip,
            )
        )
        self.db.commit()

    def reorder(self, items: list[tuple[int, int]], *, actor: str, ip: str = "") -> int:
        updated = 0
        for card_id, sort_order in items:
            card = self.cards.get_by_id(card_id)
            if card is None:
                continue
            card.sort_order = sort_order
            updated += 1
        self._log(actor, "card.reorder", None, ip, detail=f"{updated} 张卡片")
        self.db.commit()
        return updated

    def record_click(
        self, card: Card, *, actor: str, ip: str = "", endpoint_slug: str = ""
    ) -> None:
        """记一次打开。

        `endpoint_slug` 是用户点开的那条环境地址（点卡片本体时为空）。
        点击量仍记在**卡片**上 —— 用户关心的是"这个系统被打开过几次"，
        而不是"生产环境比测试环境多被点了 3 次"；具体点的是哪个环境体现在审计
        明细里，需要时能查，但不参与计数。
        """
        self.cards.increment_click(card.id)

        endpoint = self.endpoints.get_by_slug(endpoint_slug) if endpoint_slug else None
        # 传进来的标识未必属于这张卡片（比如手工构造的请求），认不出就退回卡片本体
        if endpoint is not None and endpoint.card_id != card.id:
            endpoint = None

        if endpoint is None:
            detail = f"{card.open_mode} -> {card.target_url}"
        else:
            detail = f"{endpoint.name}（{endpoint.open_mode}）-> {endpoint.url}"

        self.audit.add(
            AuditLog(
                username=actor,
                action="card.open",
                target_type="card",
                target_id=card.slug,
                detail=detail,
                ip=ip,
            )
        )
        self.db.commit()

    # ------------------------------------------------------------------
    def export_cards(self) -> CardExportDoc:
        """导出全部卡片（含已停用），用于备份 / 换机迁移 / 分享给同事。"""
        cards = self.cards.list_cards(enabled_only=False)
        items = [CardExportItem.model_validate(card) for card in cards]
        logger.info("导出 %d 张卡片配置", len(items))
        return CardExportDoc(exported_at=utcnow(), count=len(items), cards=items)

    def import_cards(
        self,
        payload: CardImportRequest,
        *,
        actor: str,
        ip: str = "",
        row_labels: Sequence[int] | None = None,
        extra_errors: Sequence[str] = (),
        total: int | None = None,
    ) -> CardImportResult:
        """批量导入。

        - `merge`：按 slug 命中则覆盖该卡片配置，未命中则新建。适合"增量同步"
        - `replace`：先清空全部卡片再按导入内容重建。适合"整机还原"
        - `dry_run`：只计算会发生的变更并回滚，让用户先看清楚再决定

        单条数据非法只跳过该条并记入 `errors`，不影响其余条目。

        **环境地址的覆盖规则**：只有当条目里**存在 `endpoints` 键**时，才认为文件
        代表了这条卡片环境地址的权威状态、执行整体替换；没有这个键就原样保留库里的
        环境地址。这条规则专门用来兜住两种情况：

        - 本功能上线前导出的老配置文件里没有 `endpoints` 键 —— 重新导入它不该
          把用户后来攒下的归档地址全抹掉；
        - 表格文件（xlsx/csv）如果没有能承载多个地址的列，表格层就不会生成这个键，
          一次"改错别字"的导入同样不会顺手清空归档。

        表格文件（xlsx/csv）导入时会额外传三个参数，让错误提示能对到用户在
        Excel 里看得见的位置：

        - `row_labels`：与 `payload.cards` 一一对应的**物理行号**
        - `extra_errors`：表格层就已判定无法使用的行（自带行号的中文文案）
        - `total`：表格里的数据行总数（含被表格层丢掉的行）
        """
        result = CardImportResult(
            mode=payload.mode,
            dry_run=payload.dry_run,
            total=total if total is not None else len(payload.cards),
        )

        # 表格层丢掉的行走在最前面：它们带的是文件里的真实行号，
        # 而下面的 Pydantic 校验只会说"第 N 条"，混在一起反而难找
        for message in extra_errors:
            self._push_error(result, message)

        parsed: list[CardExportItem] = []
        seen: set[str] = set()
        # 文件里"明确给出了环境地址"的卡片；只有这些才会覆盖库里的地址
        endpoint_authority: set[str] = set()
        for index, raw in enumerate(payload.cards, start=1):
            if not isinstance(raw, dict):
                self._record_import_error(
                    result, index, "", "条目不是 JSON 对象", row_labels=row_labels
                )
                continue
            try:
                item = CardExportItem.model_validate(raw)
            except ValidationError as exc:
                self._record_import_error(
                    result, index, self._raw_slug(raw), _brief(exc), row_labels=row_labels
                )
                continue
            if item.slug in seen:
                self._record_import_error(
                    result, index, item.slug, "文件内 slug 重复", row_labels=row_labels
                )
                continue
            reason = self._unacceptable_import_item(item)
            if reason:
                self._record_import_error(
                    result, index, item.slug, reason, row_labels=row_labels
                )
                continue
            seen.add(item.slug)
            if "endpoints" in raw:
                endpoint_authority.add(item.slug)
            parsed.append(item)

        existing = {card.slug: card for card in self.cards.list_cards(enabled_only=False)}
        next_order = self.cards.max_sort_order()

        if payload.mode == "replace":
            for card in list(existing.values()):
                self.cards.delete(card)
                result.deleted += 1
            existing.clear()

        for offset, item in enumerate(parsed, start=1):
            current = existing.get(item.slug)
            if current is not None:
                for field in _IMPORT_FIELDS:
                    setattr(current, field, getattr(item, field))
                if item.slug in endpoint_authority:
                    self._sync_endpoints(current, item.endpoints)
                result.updated += 1
                continue

            if item.sort_order:
                sort_order = item.sort_order
            else:
                next_order += 10 * offset
                sort_order = next_order
            new_card = Card(
                slug=item.slug,
                title=item.title,
                description=item.description,
                icon=item.icon,
                icon_style=item.icon_style,
                icon_url=item.icon_url,
                bg_style=item.bg_style,
                accent_color=item.accent_color,
                target_url=item.target_url,
                open_mode=item.open_mode,
                group_name=item.group_name or "默认分组",
                sort_order=sort_order,
                enabled=item.enabled,
                open_in_new_tab=item.open_in_new_tab,
                verify_tls=item.verify_tls,
            )
            self.cards.add(new_card)
            if item.endpoints:
                # 新建的卡片库里本来就没有地址，不存在"要不要保留旧的"问题
                self._sync_endpoints(new_card, item.endpoints)
            result.created += 1

        detail = (
            f"mode={payload.mode} dry_run={payload.dry_run} "
            f"created={result.created} updated={result.updated} "
            f"deleted={result.deleted} skipped={result.skipped}"
        )
        self._log(actor, "card.import", None, ip, detail=detail)

        if payload.dry_run:
            self.db.rollback()
            logger.info("导入预演完成（已回滚）：%s", detail)
        else:
            self.db.commit()
            logger.info("导入完成：%s", detail)
        return result

    @staticmethod
    def _unacceptable_import_item(item: CardExportItem) -> str:
        """导入前的地址可用性检查。通过返回空串，否则返回给用户看的原因。

        卡片自身地址与环境地址**各按自身的 open_mode** 判断：直连的地址由浏览器
        打开、根本不经过跳板机，不受 SSRF 规则约束；只有 proxy 的才需要拦。

        环境地址出错时直接判整张卡片不合格（而不是悄悄丢掉那条地址）：
        导入是"整机还原/增量同步"的场景，少一条归档地址用户很难发现，
        不如让这张卡片整体失败、在错误列表里指名道姓地说清楚是哪条地址有问题。
        """
        if item.open_mode == OPEN_MODE_PROXY:
            try:
                validate_target(item.target_url, strict_dns=False)
            except AppError as exc:
                return exc.message
        for endpoint in item.endpoints:
            if endpoint.open_mode != OPEN_MODE_PROXY:
                continue
            try:
                validate_target(endpoint.url, strict_dns=False)
            except AppError as exc:
                return f"环境地址「{endpoint.name}」：{exc.message}"
        return ""

    def _record_import_error(
        self,
        result: CardImportResult,
        index: int,
        slug: str,
        message: str,
        *,
        row_labels: Sequence[int] | None = None,
    ) -> None:
        label = self._row_label(index, row_labels)
        if slug:
            label += f"（{slug}）"
        self._push_error(result, f"{label}：{message}")

    @staticmethod
    def _row_label(index: int, row_labels: Sequence[int] | None) -> str:
        """定位提示：表格导入用文件里的物理行号，JSON 导入用条目序号。

        用行号而不是"第 N 条"，是因为用户在 Excel 里只能看到行号 ——
        表里若有几行被跳过，"第 N 条"会和实际行号错开，反而找不到。
        """
        if row_labels and 1 <= index <= len(row_labels):
            return f"第 {row_labels[index - 1]} 行"
        return f"第 {index} 条"

    def _push_error(self, result: CardImportResult, message: str) -> None:
        result.skipped += 1
        if len(result.errors) < _MAX_IMPORT_ERRORS:
            result.errors.append(message)
        elif len(result.errors) == _MAX_IMPORT_ERRORS:
            result.errors.append("…… 还有更多错误已省略")

    @staticmethod
    def _raw_slug(raw: dict) -> str:
        value = raw.get("slug")
        return str(value) if isinstance(value, (str, int)) else ""

    # ------------------------------------------------------------------
    def _log(
        self, actor: str, action: str, card: Card | None, ip: str, *, detail: str = ""
    ) -> None:
        self.audit.add(
            AuditLog(
                username=actor,
                action=action,
                target_type="card",
                target_id=str(card.id) if card else "",
                detail=detail or (card.title if card else ""),
                ip=ip,
            )
        )


def default_open_mode() -> str:
    return OPEN_MODE_PROXY


__all__ = ["CardService", "OPEN_MODE_DIRECT", "OPEN_MODE_PROXY"]
