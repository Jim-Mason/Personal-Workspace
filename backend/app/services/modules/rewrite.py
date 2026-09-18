"""把模块吐出来的内容改写成「住在中台子路径下」的样子。

这是反向代理里最容易出错的一段，所以单独成文件、只放纯函数，
方便脱离 HTTP 直接测（见 scripts/ 下的自检）。

### 要改写的四类东西

| 载体 | 例子 | 不改会怎样 |
|---|---|---|
| HTML 属性 | `<link href="/static/x.css">` | 打到中台根路径 → 404 |
| CSS url() | `url(/static/bg.png)` | 同上 |
| Location 响应头 | `302 → /template/nginx` | 浏览器跳到中台根路径 → 404 |
| Set-Cookie 的 Path | `Path=/` | 模块的会话 Cookie 撒到整个中台域 |

### 一个刻意的取舍：**不改 JSON**

`/api/templates` 这类接口返回的是模板定义和生成好的脚本内容。脚本正文里
本来就可能出现 `/etc/nginx` 这种字符串，无差别改 JSON 会把用户要的产物弄脏。
运行时那部分由注入的 pathfix.js 在前端补前缀，够用。

### 另一处取舍：脚本正文为什么不受影响

OpsGen 用 `<pre><code>{{ content }}</code></pre>` 渲染生成结果，Jinja2 默认
开启自动转义，正文里的 `"` 会变成 `&#34;`，属性正则匹配不到。这是运气好，
但也是必须验证过才敢依赖的运气 —— 所以这里只改写明确的白名单属性名，
不做「所有引号里以 / 开头的东西」这种危险的通配。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# 会被改写的 HTML 属性。刻意用白名单，不用通配。
_ATTR_RE = re.compile(
    r"(?P<pre>\b(?:href|src|action|poster|formaction|data-url|data-href|data-action)"
    r"\s*=\s*)(?P<q>[\"'])(?P<url>[^\"'>]*)(?P=q)",
    re.IGNORECASE,
)

_CSS_URL_RE = re.compile(
    r"url\(\s*(?P<q>[\"']?)(?P<url>[^\"')\s]*)(?P=q)\s*\)",
    re.IGNORECASE,
)

# 运行时跳转：`location.href = "/x"`、`location = "/x"`
_JS_LOC_ASSIGN_RE = re.compile(
    r"(?P<pre>\blocation(?:\s*\.\s*(?:href|pathname))?\s*=\s*)(?P<q>[\"'])(?P<url>/[^\"'\n]*)(?P=q)"
)

# 运行时跳转：`location.assign("/x")`、`location.replace("/x")`
_JS_LOC_CALL_RE = re.compile(
    r"(?P<pre>\blocation\s*\.\s*(?:assign|replace)\s*\(\s*)(?P<q>[\"'])(?P<url>/[^\"'\n]*)(?P=q)"
)

# 外部子资源：外链脚本与样式表。默认剥掉，见 spec.allow_external_assets。
_EXTERNAL_SUBRESOURCE_RE = re.compile(
    r"<(?P<tag>script|link)\b[^>]*\b(?P<attr>src|href)\s*=\s*[\"']"
    r"(?:https?:)?//(?!(?:127\.0\.0\.1|localhost)\b)[^\"']*[\"'][^>]*>(?:\s*</script\s*>)?",
    re.IGNORECASE,
)

_SHIM = (_ASSETS_DIR / "pathfix.js").read_text(encoding="utf-8")


def _is_prefixable(url: str) -> bool:
    """判断一个 URL 是不是「站内根路径」，是才需要补前缀。

    要排除的：相对路径、绝对 URL、协议相对、锚点、data:/mailto:/javascript:。
    """
    if not url or not url.startswith("/"):
        return False
    return not url.startswith("//")


def prefix_url(url: str, mount: str) -> str:
    if not _is_prefixable(url):
        return url
    if url == mount or url.startswith(mount + "/"):
        return url
    return mount + url


def _rewrite_origin(text: str, mount: str, origins: list[str]) -> str:
    """把指向中台自己域名的绝对 URL 补上模块前缀。

    为什么单独一条规则：Flask 的 `request.url_root` 会拼出
    `http://127.0.0.1:8731/share/xxx` 这样的完整地址（OpsGen 的分享框就是
    这么来的）。它既不是根路径、也不是外部链接，前面几条规则都碰不到它。
    不补前缀，用户复制到的分享链接就是坏链。

    负向前瞻保证已经带前缀的地址不会被补第二遍。
    """
    for origin in origins:
        pattern = re.compile(
            re.escape(origin) + r"/(?!" + re.escape(mount.lstrip("/")) + r"(?:[/?#]|$))"
        )
        text = pattern.sub(origin + mount + "/", text)
    return text


def rewrite_html(text: str, mount: str, *, origins: list[str] | None = None) -> str:
    text = _rewrite_origin(text, mount, origins or [])
    text = _ATTR_RE.sub(
        lambda m: f"{m.group('pre')}{m.group('q')}"
        f"{prefix_url(m.group('url'), mount)}{m.group('q')}",
        text,
    )
    text = _CSS_URL_RE.sub(
        lambda m: f"url({m.group('q')}{prefix_url(m.group('url'), mount)}{m.group('q')})",
        text,
    )
    text = _JS_LOC_ASSIGN_RE.sub(
        lambda m: f"{m.group('pre')}{m.group('q')}"
        f"{prefix_url(m.group('url'), mount)}{m.group('q')}",
        text,
    )
    text = _JS_LOC_CALL_RE.sub(
        lambda m: f"{m.group('pre')}{m.group('q')}"
        f"{prefix_url(m.group('url'), mount)}{m.group('q')}",
        text,
    )
    return text


def rewrite_css(text: str, mount: str) -> str:
    return _CSS_URL_RE.sub(
        lambda m: f"url({m.group('q')}{prefix_url(m.group('url'), mount)}{m.group('q')})",
        text,
    )


def strip_external_subresources(text: str) -> tuple[str, list[str]]:
    """剥掉外链脚本与样式表（默认行为，对应「零网络上传、无遥测」）。

    返回 (改写后的文本, 被剥掉的原始标签列表)，后者用于在模块日志里
    如实记下「我动了你页面的什么」—— 这类静默改动必须可追溯。
    """
    removed: list[str] = []

    def _drop(match: re.Match) -> str:
        tag = match.group(0)
        removed.append(" ".join(tag.split())[:200])
        return f"<!-- 中台已剥离外链资源：{match.group('tag')} -->"

    return _EXTERNAL_SUBRESOURCE_RE.sub(_drop, text), removed


def inject_shim(html: str, *, mount: str, realtime: bool, external_assets: bool) -> str:
    """把路径垫片插到 </head> 之前。

    插在 head 末尾而不是开头，是为了不抢在 `<meta charset>` 前面 ——
    charset 一旦被推到 1024 字节之外，中文会直接变乱码。
    而 head 末尾仍然早于 body 里的任何脚本，垫片来得及生效。
    """
    config = json.dumps(
        {"mount": mount, "realtime": bool(realtime), "externalAssets": bool(external_assets)},
        ensure_ascii=False,
    )
    snippet = (
        f'<script>window.__LOCALDECK__ = {config};</script>\n'
        f"<script>\n{_SHIM}\n</script>\n"
    )
    lowered = html.lower()
    index = lowered.rfind("</head>")
    if index >= 0:
        return html[:index] + snippet + html[index:]
    return snippet + html


def rewrite_location_header(value: str, mount: str, upstream_origin: str) -> str:
    """改写重定向目标。

    Flask 的 `redirect(url_for(...))` 给的是根路径 `/template/nginx`；
    也有模块会给出自己的完整地址 `http://127.0.0.1:8311/x`。
    两种都要落回中台的 `/opsgen/...`。
    """
    if not value:
        return value
    if value.startswith(upstream_origin):
        value = value[len(upstream_origin) :] or "/"
    return prefix_url(value, mount)


def rewrite_set_cookie(value: str, mount: str) -> str:
    """把模块会话 Cookie 的作用域收进模块自己的路径。

    Flask 默认发 `Path=/`。不放宽到 `/opsgen` 以外的好处很直接：
    模块的 Cookie 不会跟着发给中台的 /api/*，少一个被顺走的面。
    顺手补 SameSite=Lax（模块原本通常没写），降低跨站请求带上它的机会。
    """
    if not value:
        return value
    if re.search(r";\s*path\s*=", value, re.IGNORECASE):
        value = re.sub(
            r";\s*path\s*=\s*[^;]*", f"; Path={mount}", value, count=1, flags=re.IGNORECASE
        )
    else:
        value = f"{value}; Path={mount}"
    if not re.search(r";\s*samesite\s*=", value, re.IGNORECASE):
        value = f"{value}; SameSite=Lax"
    return value


def is_rewritable(content_type: str) -> str:
    """返回 'html' / 'css' / ''，判断这类响应体要不要动。"""
    lowered = (content_type or "").lower()
    if "text/html" in lowered or "application/xhtml" in lowered:
        return "html"
    if "text/css" in lowered:
        return "css"
    return ""


_MOUNT_SAFE_RE = re.compile(r"^[A-Za-z0-9._~\-/]*$")


def safe_subpath(sub: str) -> str:
    """代理转发前的路径校验。

    三条：不许 `..`（穿越）、不许反斜杠（Windows 上会被当分隔符）、
    字符集收敛。改写规则本身已经很窄，但转发前这一道必须独立成立 ——
    不能把安全寄望于「上游不会构造出奇怪的路径」。
    """
    if not _MOUNT_SAFE_RE.match(sub or ""):
        return ""
    if "\\" in sub or ".." in sub.split("/"):
        return ""
    return sub.lstrip("/")
