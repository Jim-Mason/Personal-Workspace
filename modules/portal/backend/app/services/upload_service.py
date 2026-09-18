"""图标上传：落盘 + 类型校验。

设计取舍
--------
1. **只认魔法字节，不认扩展名/Content-Type**：客户端声明的 `filename` 与
   `content-type` 都可以随手伪造，唯一可信的是文件头。
2. **拒绝 SVG**：SVG 是 XML，可以内嵌 `<script>`。虽然放在 `<img src>` 里不会执行，
   但用户完全可能直接在新标签页打开 `/uploads/icons/xxx.svg`，那就是同源下的
   任意脚本执行。图标场景用 PNG/WEBP 完全够用，因此直接拒绝并给出转换提示。
3. **文件名服务端生成**：`uuid4().hex + 扩展名`，杜绝 `../` 穿越与同名覆盖。
4. **单文件读满即停**：只读取 `max_bytes + 1` 字节即可判断是否超限，
   不会因为恶意大文件把内存打爆。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.core.config import settings
from app.core.errors import BadRequestError, ForbiddenError

logger = logging.getLogger("app.service.upload")

# 允许的图片类型：按文件头前缀匹配
# 注意 WEBP 需要特别处理（RIFF....WEBP），见 _detect()
_MAGIC_PREFIXES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"\x00\x00\x01\x00", ".ico", "image/x-icon"),
    (b"BM", ".bmp", "image/bmp"),
)

# 明确拒绝但需要给出友好提示的类型
_REJECTED_HINTS = {
    b"<svg": "SVG 图标存在脚本注入风险，请先导出为 PNG（推荐 128×128）后再上传。",
    b"<?xml": "XML/SVG 图标存在脚本注入风险，请先导出为 PNG（推荐 128×128）后再上传。",
    b"<htm": "不允许上传 HTML 文件，请上传 PNG/JPG/WEBP/GIF 图片。",
    b"<!do": "不允许上传 HTML 文件，请上传 PNG/JPG/WEBP/GIF 图片。",
}

# 允许的扩展名（仅用于服务端生成文件名，非校验依据）
ALLOWED_EXTENSIONS = {".png", ".jpg", ".gif", ".ico", ".bmp", ".webp"}


@dataclass(frozen=True)
class StoredFile:
    """落盘结果。"""

    url: str
    filename: str
    size: int
    content_type: str


def _detect(head: bytes) -> tuple[str, str] | None:
    """返回 (扩展名, MIME)，无法识别返回 None。"""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    for prefix, ext, mime in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            return ext, mime
    return None


def _reject_reason(head: bytes) -> str | None:
    lowered = head[:8].lstrip().lower()
    for prefix, hint in _REJECTED_HINTS.items():
        if lowered.startswith(prefix):
            return hint
    return None


class UploadService:
    # ------------------------------------------------------------------
    async def save_icon(self, upload: UploadFile) -> StoredFile:
        if not settings.uploads_enabled:
            raise ForbiddenError("图标上传功能已关闭（UPLOADS_ENABLED=false）")

        limit = settings.upload_max_icon_bytes
        blob = await upload.read(limit + 1)
        if not blob:
            raise BadRequestError("上传文件为空")
        if len(blob) > limit:
            raise BadRequestError(f"图标文件不能超过 {limit // 1024} KB")

        # 先按文件头判定，再考虑扩展名：客户端声称的扩展名没有意义（文件一律重命名），
        # 而文件头能给出的提示更准确（例如 SVG 会直接告诉用户"请先导出为 PNG"）。
        head = blob[:512]
        hint = _reject_reason(head)
        if hint:
            raise BadRequestError(hint)

        detected = _detect(head)
        if detected is None:
            name = (upload.filename or "").strip().lower()
            ext = f".{name.rsplit('.', 1)[-1]}" if "." in name else ""
            subject = f"不支持的文件类型 {ext}，" if ext else "无法识别的图片格式，"
            raise BadRequestError(
                f"{subject}仅支持 {' / '.join(sorted(e.lstrip('.') for e in ALLOWED_EXTENSIONS))} 图片。"
            )

        ext, mime = detected
        filename = f"{uuid.uuid4().hex}{ext}"
        target = settings.icons_path / filename

        # 原子写：先写 .part 再改名，避免读到写了一半的文件
        part = target.with_suffix(target.suffix + ".part")
        part.write_bytes(blob)
        part.replace(target)

        logger.info("图标已保存 %s (%d bytes, %s)", filename, len(blob), mime)
        return StoredFile(
            url=f"/uploads/icons/{filename}",
            filename=filename,
            size=len(blob),
            content_type=mime,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def resolve_icon(filename: str) -> Path:
        """把文件名安全地解析成绝对路径（防路径穿越），供后续清理逻辑复用。"""
        safe = (filename or "").strip().replace("\\", "/").split("/")[-1]
        if not safe or safe.startswith(".") or ".." in safe:
            raise BadRequestError("非法的文件名")

        base = settings.icons_path.resolve()
        target = (base / safe).resolve()
        if base != target.parent:
            raise BadRequestError("非法的文件路径")
        return target


__all__ = ["UploadService", "StoredFile", "ALLOWED_EXTENSIONS"]
