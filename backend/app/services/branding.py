"""品牌与外观配置：平台叫什么、长什么样，全部由用户自己定。

三个设计取舍，写在这里免得下次又重新纠结：

1. **配置存 SQLite 的 meta 表，不落独立 json 文件。**
   README 已经承诺「备份只需拷一个 db 文件」，再加一个 branding.json 就把这个承诺破了。

2. **图片以 data URL 内联进 CSS 变量，不落地成图片文件。**
   同样是为了上面那条承诺，顺带免掉一整套静态资源路由和它的鉴权例外。
   代价是首页 HTML 会随图片变大——本地回环传输，几毫秒的事。
   所以设了体积上限（见 MAX_*），挡住「手滑传了张 20MB 壁纸」这种情况。

3. **只注入几个基色，派生色交给 CSS 的 color-mix() 现算。**
   这样用户拖动取色器时，浅色底、描边、阴影会跟着实时变，
   不需要前端 JS 再算一遍，也不需要在两种语言里维护同一套配色公式。

安全上有一条硬规则：**只接受位图，拒绝 SVG**。
SVG 能内嵌脚本，虽然用作 CSS background-image 时不会执行，
但少一个可以被绕的入口，比事后解释为什么安全要划算。
"""

from __future__ import annotations

import json
import re

from ..db import get_conn

_META_KEY = "branding"

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_IMAGE_RE = re.compile(r"^data:image/(png|jpe?g|gif|webp|bmp);base64,[A-Za-z0-9+/=\s]+$")
_REMOTE_RE = re.compile(r"^https?://", re.IGNORECASE)

# data URL 是原图体积的约 4/3，这两个上限对应原图约 1.2MB / 0.65MB
MAX_BG_IMAGE_CHARS = 1_650_000
MAX_LOGO_IMAGE_CHARS = 900_000
MAX_NAME_CHARS = 32
MAX_TAGLINE_CHARS = 48
MAX_LOGO_TEXT_CHARS = 4

MODES = ("light", "dark")
BG_STYLES = ("aura", "grid", "plain")

# 两套「底色默认值」。切换明暗时，若当前值仍等于本模式的默认值，
# 说明用户没动过它，就顺手换成另一套默认值；否则保留用户的选择。
LIGHT_BASE = {"bg": "#f4f5fb", "surface": "#ffffff", "text": "#1b1b25"}
DARK_BASE = {"bg": "#0b0d14", "surface": "#151823", "text": "#eceef6"}

# 预设只决定「主色 + 副色」。底色/面板色/明暗由用户单独控制，
# 否则换一个预设就把自定义的底色冲掉了，手感很差。
PRESETS: dict[str, dict[str, str]] = {
    "aurora": {"label": "极光", "accent": "#6366f1", "accent2": "#22d3ee"},
    "ocean": {"label": "深海", "accent": "#0ea5e9", "accent2": "#14b8a6"},
    "sunset": {"label": "日落", "accent": "#f97316", "accent2": "#ec4899"},
    "forest": {"label": "森绿", "accent": "#0f6e56", "accent2": "#84cc16"},
    "violet": {"label": "紫罗兰", "accent": "#8b5cf6", "accent2": "#f472b6"},
    "graphite": {"label": "石墨", "accent": "#475569", "accent2": "#0ea5e9"},
}

DEFAULTS: dict = {
    "app_name": "Personal Workspace",
    "tagline": "本机资产 · 运维工具 · 内网门户",
    "logo_text": "PW",
    "logo_image": "",
    "mode": "light",
    "preset": "aurora",
    "accent": PRESETS["aurora"]["accent"],
    "accent2": PRESETS["aurora"]["accent2"],
    "bg": LIGHT_BASE["bg"],
    "surface": LIGHT_BASE["surface"],
    "text": LIGHT_BASE["text"],
    "bg_image": "",
    "bg_overlay": 52,
    "bg_style": "aura",
    # 前端用：默认值本身（恢复默认时不必再问一次服务端）
    "presets": PRESETS,
    "defaults_locked": False,
}


class BrandingError(ValueError):
    """配置不合法。消息直接给用户看，所以要写人话。"""


# ------------------------------------------------------------------ 校验


def _clean_text(raw, field: str, limit: int) -> str:
    if raw is None:
        return ""
    text = str(raw).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        raise BrandingError(f"{field} 最多 {limit} 个字符，当前 {len(text)} 个")
    return text


def _clean_color(raw, field: str, fallback: str) -> str:
    if raw is None or raw == "":
        return fallback
    text = str(raw).strip()
    if not _HEX_RE.match(text):
        raise BrandingError(f"{field} 必须是 #RRGGBB 形式的颜色，收到的是「{text[:20]}」")
    return text.lower()


def _clean_image(raw, field: str, limit: int) -> str:
    """空串表示不用图；其余只接受「位图 data URL」或 http(s) 直链。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if text.startswith("data:"):
        if "svg" in text[:60].lower():
            raise BrandingError(f"{field} 不接受 SVG（可内嵌脚本），请换 PNG/JPG/WebP")
        if not _IMAGE_RE.match(text):
            raise BrandingError(f"{field} 只支持 PNG / JPG / GIF / WebP / BMP 图片")
        if len(text) > limit:
            raise BrandingError(
                f"{field} 太大了（约 {len(text) // 1024} KB），"
                f"上限约 {limit // 1024} KB，请先压缩一下再传"
            )
        return text
    if _REMOTE_RE.match(text):
        return text
    raise BrandingError(f"{field} 要么上传图片，要么填 http(s) 开头的网址")


def _clean_choice(raw, field: str, allowed: tuple[str, ...], fallback: str) -> str:
    if raw is None or raw == "":
        return fallback
    text = str(raw).strip().lower()
    if text not in allowed:
        raise BrandingError(f"{field} 只能是 {' / '.join(allowed)} 之一")
    return text


def _clean_overlay(raw, fallback: int) -> int:
    if raw is None or raw == "":
        return fallback
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise BrandingError("背景蒙层必须是一个 0-90 的数字")
    return max(0, min(90, value))


# ------------------------------------------------------------------ 读写


def _merge_defaults(stored: dict | None) -> dict:
    data = dict(DEFAULTS)
    if stored:
        for key in DEFAULTS:
            if key in ("presets", "defaults_locked"):
                continue
            if key in stored:
                data[key] = stored[key]
    return data


def load() -> dict:
    """读取当前品牌配置。任何异常都退回默认值——首页渲染不能因为配置坏了就打不开。"""
    stored: dict | None = None
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (_META_KEY,)).fetchone()
        if row and row["value"]:
            parsed = json.loads(row["value"])
            if isinstance(parsed, dict):
                stored = parsed
    except Exception:  # noqa: BLE001 —— 配置损坏不该让平台起不来
        stored = None
    return _merge_defaults(stored)


def save(patch: dict) -> dict:
    """局部更新。只认识白名单里的字段，其余一律忽略。"""
    if not isinstance(patch, dict):
        raise BrandingError("请求体必须是一个 JSON 对象")

    current = load()
    next_data = dict(current)

    if "app_name" in patch:
        name = _clean_text(patch["app_name"], "平台名称", MAX_NAME_CHARS)
        next_data["app_name"] = name or DEFAULTS["app_name"]
    if "tagline" in patch:
        next_data["tagline"] = _clean_text(patch["tagline"], "副标题", MAX_TAGLINE_CHARS)
    if "logo_text" in patch:
        text = _clean_text(patch["logo_text"], "Logo 文字", MAX_LOGO_TEXT_CHARS)
        next_data["logo_text"] = text or DEFAULTS["logo_text"]

    # 明暗切换：先判断「用户是否动过底色」，再决定要不要一起换。
    if "mode" in patch:
        mode = _clean_choice(patch["mode"], "明暗模式", MODES, current["mode"])
        if mode != current["mode"]:
            from_base, to_base = (LIGHT_BASE, DARK_BASE) if mode == "dark" else (DARK_BASE, LIGHT_BASE)
            for key in ("bg", "surface", "text"):
                if str(current.get(key, "")).lower() == from_base[key].lower():
                    next_data[key] = to_base[key]
        next_data["mode"] = mode

    if "preset" in patch:
        preset = _clean_choice(patch["preset"], "主题预设", tuple(PRESETS), current["preset"])
        next_data["preset"] = preset
        # 预设只在用户没显式给色值时才生效，否则「点预设」会把人手调的颜色吃掉
        if "accent" not in patch:
            next_data["accent"] = PRESETS[preset]["accent"]
        if "accent2" not in patch:
            next_data["accent2"] = PRESETS[preset]["accent2"]

    if "accent" in patch:
        next_data["accent"] = _clean_color(patch["accent"], "主色", current["accent"])
        if "preset" not in patch:
            next_data["preset"] = "custom"
    if "accent2" in patch:
        next_data["accent2"] = _clean_color(patch["accent2"], "副色", current["accent2"])
        if "preset" not in patch and next_data["preset"] != "custom":
            next_data["preset"] = "custom"

    if "bg" in patch:
        next_data["bg"] = _clean_color(patch["bg"], "底色", current["bg"])
    if "surface" in patch:
        next_data["surface"] = _clean_color(patch["surface"], "面板色", current["surface"])
    if "text" in patch:
        next_data["text"] = _clean_color(patch["text"], "文字色", current["text"])

    if "bg_image" in patch:
        next_data["bg_image"] = _clean_image(patch["bg_image"], "背景图", MAX_BG_IMAGE_CHARS)
    if "logo_image" in patch:
        next_data["logo_image"] = _clean_image(patch["logo_image"], "Logo 图片", MAX_LOGO_IMAGE_CHARS)

    if "bg_overlay" in patch:
        next_data["bg_overlay"] = _clean_overlay(patch["bg_overlay"], current["bg_overlay"])
    if "bg_style" in patch:
        next_data["bg_style"] = _clean_choice(patch["bg_style"], "背景样式", BG_STYLES, current["bg_style"])

    _persist(next_data)
    return _merge_defaults(next_data)


def reset() -> dict:
    _persist(dict(DEFAULTS))
    return _merge_defaults(None)


def _persist(data: dict) -> None:
    payload = {k: v for k, v in data.items() if k not in ("presets",)}
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO meta(key, value, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(key) DO UPDATE
               SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (_META_KEY, json.dumps(payload, ensure_ascii=False)),
        )


# ------------------------------------------------------------------ 输出


def to_css(data: dict) -> str:
    """把配置变成一段 :root 变量。

    服务端直接注入首页，页面带着正确配色首屏渲染 ——
    否则会出现「先闪一下默认蓝紫，再跳成用户配色」，很难看。
    """
    bg_image = data.get("bg_image") or ""
    logo_image = data.get("logo_image") or ""
    overlay = max(0, min(90, int(data.get("bg_overlay", 52)))) / 100.0

    def url_var(value: str) -> str:
        # 双引号是 CSS url() 的合法定界符，data URL 里不会出现未转义的双引号
        return f'url("{value}")' if value else "none"

    lines = [
        f"--accent: {data['accent']};",
        f"--accent-2: {data['accent2']};",
        f"--bg: {data['bg']};",
        f"--surface: {data['surface']};",
        f"--text: {data['text']};",
        f"--bg-image: {url_var(bg_image)};",
        f"--bg-overlay: {overlay:.2f};",
        f"--brand-logo: {url_var(logo_image)};",
    ]
    return ":root{" + "".join(lines) + "}"


def public_payload(data: dict) -> dict:
    """给前端的完整配置（含预设表，供外观抽屉渲染色卡）。"""
    payload = _merge_defaults(data)
    payload["presets"] = PRESETS
    payload["light_base"] = LIGHT_BASE
    payload["dark_base"] = DARK_BASE
    payload["limits"] = {
        "bg_image_kb": MAX_BG_IMAGE_CHARS // 1024,
        "logo_image_kb": MAX_LOGO_IMAGE_CHARS // 1024,
    }
    return payload
