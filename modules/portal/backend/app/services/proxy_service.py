"""反向代理网关：把本地浏览器对内网地址的访问，经由跳板机转发出去。

设计要点
--------
* 统一前缀 `/gw/{slug}/`，被代理页面内的根路径资源会被重写到该前缀下，
  因此内网系统的 CSS/JS 也能正常加载。
* **静态标记 + 运行时请求，两条路都要堵**：
  - HTML/CSS 里的 `src="/x"` `url(/x)` → 字节级重写（本模块 `rewrite_html` / `rewrite_css`）
  - JS 运行时拼出来的 `fetch("/x")` → 注入客户端 shim（`client_shim`）
  - shim 也拦不住的顶层导航（`location.href="/x"`）→ 服务端按 Referer 兜底转发
    （`gateway_slug_from_referer` + main.py 的 fallback 路由）
  只做第一条会漏掉一半 —— 现代前端几乎全是运行时拼 URL。
* 认证 Cookie（dtb_*）绝不转发给内网目标，避免把门户令牌泄露给第三方系统。
* 上游连接失败 / 5xx 时返回友好的 HTML 错误页，而不是 JSON 或裸栈。
* 支持 WebSocket 透传（ws/wss），用于内网系统的实时推送页面。

两条响应路径
------------
1. `text/html` 且开启重写 → 整块读取（httpx 自动解压）→ 字节级重写 → Response
2. 其它内容 → 原样流式透传（保持 content-encoding，零拷贝吃大流量）

字节级（而非文本级）重写是刻意选择：老内网系统常用 GBK 编码，
按 bytes 处理可以完全规避编解码错误。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from html import escape
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import ACCESS_COOKIE, REFRESH_COOKIE
from app.models.entities import OPEN_MODE_PROXY, Card, CardEndpoint
from app.services.proxy_guard import validate_target

logger = logging.getLogger("app.proxy")

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# 由 httpx / 我们自行重建，不从客户端透传
_REQUEST_DROP = _HOP_BY_HOP | {
    "host",
    "content-length",
    "cookie",  # 单独处理：剥离门户自身 Cookie 后再转发
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-forwarded-prefix",
    "forwarded",
}

# 由我们重写或不应透传
_RESPONSE_DROP = _HOP_BY_HOP | {"content-length", "location", "set-cookie"}

_SECURITY_HEADERS_TO_STRIP = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
}

_HEAD_RE = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
_BASE_TAG_RE = re.compile(rb"<base\b[^>]*>", re.IGNORECASE)
_ATTR_URL_RE = re.compile(
    rb"(?P<pre>\b(?:src|href|action|poster|data-src|formaction)\s*=\s*)"
    rb"(?P<q>[\"'])(?P<url>/(?!/)[^\"']*)(?P=q)",
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(
    rb"(?P<pre>url\(\s*)(?P<q>[\"']?)(?P<url>/(?!/)[^\"')\s]*)(?P=q)(?P<post>\s*\))",
    re.IGNORECASE,
)
# 绝对地址（含协议相对）：`http(s)://<host>/x`、`//<host>/x`
_ABS_ATTR_URL_RE = re.compile(
    rb"(?P<pre>\b(?:src|href|action|poster|data-src|formaction)\s*=\s*)"
    rb"(?P<q>[\"'])(?P<url>(?:https?:)?//[^\"'\s<>]+)(?P=q)",
    re.IGNORECASE,
)
_ABS_CSS_URL_RE = re.compile(
    rb"(?P<pre>url\(\s*)(?P<q>[\"']?)(?P<url>(?:https?:)?//[^\"')\s]+)(?P=q)(?P<post>\s*\))",
    re.IGNORECASE,
)
_META_CSP_RE = re.compile(
    rb"<meta\b[^>]*http-equiv\s*=\s*[\"']?content-security-policy[\"']?[^>]*>", re.IGNORECASE
)

# ----------------------------------------------------------------------
# 客户端运行时 shim：补 `<base>` 覆盖不到的「根路径绝对地址」
# ----------------------------------------------------------------------
# 为什么需要它
# ------------
# `<base href="/gw/x/">` 只影响**相对路径**（`a/b`、`./a`）。
# 对根路径绝对地址（`/api/v1/config`）完全无效 —— 浏览器会直接打到门户域名根上，
# 于是内网页面里的 `fetch("/api/v1/xxx")` 会命中门户自己的路由并 404。
# 而这类地址是 JS **运行时拼出来**的，字节级 HTML 重写根本看不到它。
#
# 因此在页面最前面注入一段 shim，重写所有"运行时发出的根路径请求"：
# fetch / XHR / WebSocket / EventSource / Worker / window.open / sendBeacon /
# setAttribute / 常见元素的 URL 属性 setter。
#
# 顺带覆盖第二类：**与目标同源的绝对地址**（`http://<目标host>/x`）。
# 例如 Jenkins 用 `Server.getRootUrl()` 生成 `http://10.0.0.20:8081/theme-dark/theme.css`，
# 这类地址不属于"外链"，但客户端在跳板机场景下根本访问不到那个 host：
# 浏览器会绕过网关直连，请求直接失败（status=0），页面的主题 CSS 就没了。
# 所以同源绝对地址也要还原成网关路径（`__ORIGIN__` / `__BASEPATH__`）。
#
# 仍然覆盖不到、改由服务端兜底（见 main.py 的 fallback 路由）的情况：
#   - `location.href = "/x"` 这类顶层导航（window.location 是 [Unforgeable]，改不了）
#   - worker 内部再 new 出来的嵌套 worker
#   - 内联脚本里硬编码的绝对地址（只有经过 fetch/XHR/属性赋值等入口才拦得住）
#
# ⚠️ 下面这段 JS **必须是纯 ASCII**：
# 它会被原样注入到被代理页面里，而老内网系统可能是 GBK 编码的。
# 注入 UTF-8 的中文注释会直接破坏整个页面的编码（字符串本身也一样，
# 但要避免只因为"注释看起来更清楚"就把页面搞坏）。
# 这条约束由 `client_shim()` 里的断言 + e2e 用例共同守住。
_CLIENT_SHIM_JS = r"""
(function(){
  var P=__PREFIX__, B=P.replace(/\/$/,""), O=__ORIGIN__, BP=__BASEPATH__;
  var abs=/^\/(?!\/)/;                    /* leading slash, but not protocol-relative //host */
  var A={src:1,href:1,action:1,poster:1,"data-src":1,formaction:1,"xlink:href":1};
  function own(u){
    /* same-origin absolute URL -> root-relative path; null when it is not ours */
    var s=u, o=O||"";
    if(s.charAt(0)==="/"&&s.charAt(1)==="/"){
      var r=s.slice(2), i=r.indexOf("/"), h=(i<0?r:r.slice(0,i));
      if(!o)return null;
      if(h.toLowerCase()!==o.slice(o.indexOf("//")+2))return null;
      s=(i<0?"/":r.slice(i));
    }else if(o&&s.length>o.length&&s.slice(0,o.length).toLowerCase()===o){
      var n=s.charAt(o.length);
      if(n!=="/"&&n!=="?"&&n!=="#")return null;
      s=s.slice(o.length);
    }else{return null;}
    if(BP&&BP!=="/"){
      if(s===BP.slice(0,BP.length-1))s="/";
      else if(s.indexOf(BP)===0)s="/"+s.slice(BP.length);
      else return null;                   /* same origin but outside the proxied directory */
    }
    return s;
  }
  function fx(u){
    if(typeof u!=="string"||!u)return u;
    var s=u;
    if(!abs.test(s)){
      var c=own(s);
      /* relative URL (handled by <base>) or an external site: leave it alone */
      if(c===null)return u;
      s=c;
    }
    if(s.lastIndexOf(P,0)===0)return u;   /* already prefixed: idempotent */
    if(s===B)return P;
    return B+s;
  }
  function setter(proto,prop){
    try{
      var d=Object.getOwnPropertyDescriptor(proto,prop);
      if(!d||!d.set||!d.get)return;
      Object.defineProperty(proto,prop,{configurable:true,enumerable:d.enumerable,
        get:function(){return d.get.call(this);},
        set:function(v){try{v=fx(v);}catch(e){}d.set.call(this,v);}});
    }catch(e){}
  }
  function ctor(name){
    try{
      var O=window[name];if(!O)return;
      var N=function(a,b){try{a=fx(a);}catch(e){}
        return b===undefined?new O(a):new O(a,b);};
      N.prototype=O.prototype;
      N.CONNECTING=O.CONNECTING;N.OPEN=O.OPEN;
      N.CLOSING=O.CLOSING;N.CLOSED=O.CLOSED;
      window[name]=N;
    }catch(e){}
  }
  try{var of=window.fetch;if(of){window.fetch=function(i,n){
    try{
      if(typeof i==="string")i=fx(i);
      else if(i&&typeof i.url==="string"){var f=fx(i.url);if(f!==i.url)i=new Request(f,i);}
    }catch(e){}
    return of.call(this,i,n);};}}catch(e){}
  try{var xo=XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open=function(){try{arguments[1]=fx(arguments[1]);}catch(e){}
      return xo.apply(this,arguments);};}catch(e){}
  try{var so=Element.prototype.setAttribute;
    Element.prototype.setAttribute=function(n,v){
      try{if(A[String(n).toLowerCase()])v=fx(v);}catch(e){}
      return so.call(this,n,v);};}catch(e){}
  setter(HTMLScriptElement.prototype,"src");
  setter(HTMLImageElement.prototype,"src");
  setter(HTMLIFrameElement.prototype,"src");
  setter(HTMLMediaElement.prototype,"src");
  setter(HTMLSourceElement.prototype,"src");
  setter(HTMLLinkElement.prototype,"href");
  setter(HTMLAnchorElement.prototype,"href");
  setter(HTMLAreaElement.prototype,"href");
  setter(HTMLFormElement.prototype,"action");
  setter(HTMLObjectElement.prototype,"data");
  ctor("WebSocket");
  ctor("EventSource");
  ctor("Worker");
  try{var wo=window.open;window.open=function(){try{arguments[0]=fx(arguments[0]);}catch(e){}
    return wo.apply(this,arguments);};}catch(e){}
  try{if(navigator.sendBeacon){var sb=navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon=function(u,d){try{u=fx(u);}catch(e){}return sb(u,d);};}}catch(e){}
  try{if(navigator.serviceWorker&&navigator.serviceWorker.register){
    var sr=navigator.serviceWorker.register.bind(navigator.serviceWorker);
    navigator.serviceWorker.register=function(u,o){try{u=fx(u);}catch(e){}
      return o===undefined?sr(u):sr(u,o);};}}catch(e){}
  window.__dtbGatewayPrefix=P;
})();
"""

# 用属性做标记：测试用来断言注入，也用来保证重复重写不会注入两次
_SHIM_MARK = b"data-dtb-gateway"

# 门户自身拥有的命名空间 —— 兜底转发必须避开它们。
# 注意不能把整个 `/api` 都算进来：内网系统用 `/api/v1/xxx` 是很常见的。
_PORTAL_OWNED = (
    "api/auth",
    "api/cards",
    "api/users",
    "api/uploads",
    "api/health",
    "api/docs",
    "api/openapi.json",
    "gw",
    "uploads",
    "assets",
    "health",
    "ready",
    "favicon.ico",
)


def client_shim(prefix: str, *, target_url: str = "") -> bytes:
    """生成注入到被代理页面的 shim（含 `<script>` 标签）。

    返回值保证是**纯 ASCII** —— 被代理页面可能是 GBK 编码的，
    注入 UTF-8 字节会破坏整页编码（见 `_CLIENT_SHIM_JS` 上方的说明）。

    `target_url` 用于识别"与目标同源的绝对地址"；不传时只处理根路径绝对地址。
    """
    origin, base_path = _url_origin_and_base(target_url)
    literal = json.dumps(prefix)  # 合法 JS 字符串字面量，从根上防注入
    js = _CLIENT_SHIM_JS.replace("__PREFIX__", literal, 1)
    js = js.replace("__ORIGIN__", json.dumps(origin), 1)
    js = js.replace("__BASEPATH__", json.dumps(base_path), 1)
    tag = f"<script {_SHIM_MARK.decode()}={literal}>{js}</script>"
    blob = tag.encode("ascii")  # 不是 ascii 就说明模板里混进了非 ASCII 字符
    return blob


def is_portal_owned_path(path: str) -> bool:
    """路径是否属于门户自身的命名空间（用于兜底转发前的排除判断）。"""
    clean = "/" + (path or "").strip("/")
    root = (settings.root_path or "").rstrip("/")
    if root and (clean == root or clean.startswith(root + "/")):
        clean = clean[len(root) :] or "/"
    clean = clean.split("?", 1)[0].split("#", 1)[0]
    if clean == "/":
        return True
    for item in _PORTAL_OWNED:
        if clean == f"/{item}" or clean.startswith(f"/{item}/"):
            return True
    return False


def gateway_slug_from_referer(referer: str) -> str | None:
    """从 Referer 里解析出「这个请求是哪个内网页面发出的」。

    兜底转发时，Referer 是唯一能说明归属的信号 —— 而且它天然按标签页隔离，
    不会像 Cookie 那样被多个标签页互相覆盖。
    """
    raw = (referer or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    path = parts.path if (parts.scheme or parts.netloc) else raw.split("?", 1)[0].split("#", 1)[0]
    root = (settings.root_path or "").rstrip("/")
    if root and path.startswith(root + "/"):
        path = path[len(root) :]
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) >= 2 and segments[0] == "gw":
        return unquote(segments[1]) or None
    return None


# ----------------------------------------------------------------------
# 路径 / URL 工具
# ----------------------------------------------------------------------
def _norm_netloc(netloc: str, scheme: str) -> str:
    """归一化 host:port —— 省掉默认端口，避免 `host` 与 `host:80` 被当成两个源。"""
    host = (netloc or "").lower()
    scheme = (scheme or "").lower()
    if scheme == "http" and host.endswith(":80"):
        return host[:-3]
    if scheme == "https" and host.endswith(":443"):
        return host[:-4]
    return host


def _url_origin_and_base(target_url: str) -> tuple[str, str]:
    """从卡片目标地址解析出 (归一化 origin, 作用域路径)。

    作用域由卡片地址**是否以 `/` 结尾**决定（与表单里的说明一致），
    并且与 `_join_target` 共用同一套规则，两者绝不会各走各的：

    - `http://host/app/`（**目录作用域**）：网关路径相对该目录
      `/gw/x/a.css` → `http://host/app/a.css`
    - `http://host/login`（**页面作用域**）：入口就是这个页面本身，
      `/gw/x/a.css` → `http://host/a.css`（按站点绝对路径解析）

    页面作用域是为真实场景补的：Jenkins 的入口是 `…:8081/login`
    （它的站点根 `/` 反而返回 403），若按目录作用域处理，
    页面里的 `/static/**` 会被拼成 `/login/static/**` 全部 404、
    连入口 `/login/` 自己也是 404 —— 整页直接是坏的。
    """
    raw = (target_url or "").strip()
    if not raw:
        return "", "/"
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    netloc = _norm_netloc(parts.netloc, scheme)
    origin = f"{scheme}://{netloc}" if netloc else ""
    if not raw.endswith("/"):
        return origin, "/"  # 页面作用域：子路径一律按站点绝对路径
    return origin, (parts.path or "/")


def _rewrite_absolute_url(
    url: str, *, origin: str, base_path: str, prefix_bare: str
) -> str | None:
    """把与目标同源的绝对地址改写到网关前缀下；不同源返回 None（保持外链原样）。

    这是为了处理"页面自己拼出完整 URL"的情况（Jenkins 的主题 CSS 就是典型）：
    在跳板机场景下客户端根本访问不到目标 host，外链语义在这里是不成立的。
    """
    if not origin or not url:
        return None

    raw = url.strip()
    protocol_relative = raw.startswith("//")
    if protocol_relative:
        # 协议相对地址（//host/x）的协议由当前页面决定，比较时忽略协议
        parts = urlsplit(f"{urlsplit(origin).scheme or 'http'}:{raw}")
    else:
        parts = urlsplit(raw)

    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return None
    netloc = _norm_netloc(parts.netloc, scheme)
    if not netloc:
        return None

    origin_netloc = origin.split("://", 1)[-1]
    if protocol_relative:
        if netloc != origin_netloc:
            return None
    elif f"{scheme}://{netloc}" != origin:
        return None

    path = parts.path or "/"
    if base_path != "/":
        if path == base_path.rstrip("/"):
            rest = ""
        elif path.startswith(base_path):
            rest = path[len(base_path) :]
        else:
            return None  # 同源但落在被代理目录之外，交给上游更安全
    else:
        rest = path.lstrip("/")

    out = f"{prefix_bare}/{rest}" if rest else f"{prefix_bare}/"
    if parts.query:
        out += f"?{parts.query}"
    if parts.fragment:
        out += f"#{parts.fragment}"
    return out


def gateway_prefix(slug: str) -> str:
    """被代理内容在门户下的路径前缀，如 `/gw/zentaophp/`。"""
    root = (settings.root_path or "").rstrip("/")
    return f"{root}/gw/{quote(slug)}/"


def build_launch_url(target: Card | CardEndpoint) -> str:
    """给前端用的入口地址。

    `target` 既可以是卡片、也可以是卡片下的一条环境地址：两者都有
    `slug` / `open_mode` / `target_url` 三个属性（`CardEndpoint` 用 property
    给 `url` 起了 `target_url` 别名），所以这一段逻辑可以原样复用。

    direct → 直接给原始地址（浏览器直连，不经过跳板机）
    proxy  → 给 `/gw/{slug}/`，网关拿这个标识反查真正的上游地址
    """
    if target.open_mode == OPEN_MODE_PROXY:
        return gateway_prefix(target.slug)
    return target.target_url


def _join_target(base_url: str, path: str, query: str) -> str:
    """把网关路径拼成真正的上游地址。

    **入参 `path` 为空表示"打开卡片入口"**，此时原样使用卡片地址 ——
    `http://host/login` 与 `http://host/login/` 在不少系统里是两个不同的页面
    （Jenkins 就是：前者 200，后者 404），不能自作主张补斜杠。

    其余情况按 `_url_origin_and_base` 给出的作用域解析，见该函数的说明。
    """
    origin, _ = _url_origin_and_base(base_url)

    if not path:
        target = base_url
    elif not base_url.endswith("/"):
        # 页面作用域：网关路径就是目标站点上的绝对路径
        target = f"{origin}/{path.lstrip('/')}"
    else:
        target = urljoin(base_url, path.lstrip("/"))

    if query:
        parts = urlsplit(target)
        merged = f"{parts.query}&{query}" if parts.query else query
        target = urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))
    return target


def _forwarded_for(request: Request) -> str:
    client = request.client.host if request.client else ""
    incoming = request.headers.get("x-forwarded-for", "").strip()
    return f"{incoming}, {client}".strip(", ") if incoming else client


# ----------------------------------------------------------------------
# Cookie 处理
# ----------------------------------------------------------------------
def _filter_cookie_header(raw_cookie: str, slug: str) -> str:
    """剥离门户自身认证 Cookie；开了命名空间时把前缀还原回去。"""
    if not raw_cookie:
        return ""
    portal_cookies = {ACCESS_COOKIE, REFRESH_COOKIE}
    namespace_prefix = f"gw_{slug}__"
    kept: list[str] = []
    for chunk in raw_cookie.split(";"):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name in portal_cookies:
            continue
        if settings.proxy_cookie_namespace and name.startswith(namespace_prefix):
            name = name[len(namespace_prefix) :]
        kept.append(f"{name}={value}")
    return "; ".join(kept)


def _rewrite_set_cookie(value: str, *, slug: str, prefix: str, secure_ok: bool) -> str:
    """修正上游 Set-Cookie：去 Domain、按前缀重写 Path、必要时降级 Secure/SameSite。"""
    segments = [seg.strip() for seg in value.split(";")]
    if not segments:
        return value

    name, _, rest = segments[0].partition("=")
    out: list[str] = [f"{_namespace_cookie_name(slug, name.strip())}={rest}"]

    for attribute in segments[1:]:
        if not attribute:
            continue
        attr_name, _, attr_value = attribute.partition("=")
        key = attr_name.strip().lower()
        if key == "domain":
            continue  # 门户与目标不同域，保留 Domain 浏览器会直接拒收
        if key == "path":
            raw_path = attr_value.strip() or "/"
            joined = raw_path if raw_path.startswith("/") else "/" + raw_path
            merged = (prefix.rstrip("/") + joined).replace("//", "/")
            out.append(f"Path={merged}")
            continue
        if key == "secure":
            if secure_ok:
                out.append("Secure")
            continue
        if key == "samesite":
            if attr_value.strip().lower() == "none" and not secure_ok:
                out.append("SameSite=Lax")
            else:
                out.append(f"SameSite={attr_value.strip()}")
            continue
        out.append(attribute)

    if not any(seg.lower().startswith("path=") for seg in out):
        out.append(f"Path={prefix}")
    return "; ".join(out)


def _namespace_cookie_name(slug: str, name: str) -> str:
    return f"gw_{slug}__{name}" if settings.proxy_cookie_namespace else name


def _rewrite_location(location: str, *, prefix: str, target_url: str) -> str:
    """把上游重定向地址改写到门户前缀下。"""
    if not location:
        return location
    prefix_bare = prefix.rstrip("/")
    if location.startswith("//") or location.lower().startswith(("http://", "https://")):
        origin, base_path = _url_origin_and_base(target_url)
        rewritten = _rewrite_absolute_url(
            location, origin=origin, base_path=base_path, prefix_bare=prefix_bare
        )
        return rewritten if rewritten is not None else location  # 站外：原样放行
    if location.startswith("/"):
        return prefix_bare + location
    return location  # 相对路径交给浏览器的 <base> 解析


# ----------------------------------------------------------------------
# HTML 重写
# ----------------------------------------------------------------------
def _abs_rewrite_ready(*, origin: str, base_path: str, prefix_bare: str) -> bool:
    """绝对地址改写只在「全 ASCII」时才开启。

    这一路改写是在 bytes 上做的，用到 latin-1 的 1:1 字节映射；
    只要有一个组件含非 ASCII 字符就没法安全还原回字节，
    此时宁可不改写（退回"外链原样放行"的老行为），也不能把页面写坏。
    """
    return bool(origin) and origin.isascii() and base_path.isascii() and prefix_bare.isascii()


def _rewrite_css_urls(
    body: bytes,
    *,
    prefix_bare: bytes,
    prefix_bytes: bytes,
    origin: str = "",
    base_path: str = "/",
    abs_ready: bool = False,
) -> bytes:
    def _sub(match: re.Match[bytes]) -> bytes:
        url = match.group("url")
        if url.startswith(prefix_bytes) or url.startswith(b"/gw/"):
            return match.group(0)
        return (
            match.group("pre")
            + match.group("q")
            + prefix_bare
            + url
            + match.group("q")
            + match.group("post")
        )

    body = _CSS_URL_RE.sub(_sub, body)
    if not abs_ready:
        return body

    abs_prefix = prefix_bare.decode("latin-1")

    def _sub_abs(match: re.Match[bytes]) -> bytes:
        raw = match.group("url").decode("latin-1")
        rewritten = _rewrite_absolute_url(
            raw, origin=origin, base_path=base_path, prefix_bare=abs_prefix
        )
        if rewritten is None:
            return match.group(0)
        return (
            match.group("pre")
            + match.group("q")
            + rewritten.encode("latin-1")
            + match.group("q")
            + match.group("post")
        )

    return _ABS_CSS_URL_RE.sub(_sub_abs, body)


def rewrite_html(body: bytes, *, prefix: str, target_url: str = "") -> bytes:
    """注入 <base> 并把根路径资源改写到网关前缀下。

    `target_url` 用于把**与目标同源的绝对地址**也收进网关（见 `_rewrite_absolute_url`）。
    不传时只处理根路径绝对地址，行为与早期版本一致。
    """
    if not body:
        return body

    prefix_bytes = prefix.encode("utf-8")
    prefix_bare = prefix_bytes.rstrip(b"/")
    origin, base_path = _url_origin_and_base(target_url)
    abs_ready = _abs_rewrite_ready(
        origin=origin, base_path=base_path, prefix_bare=prefix_bare.decode("latin-1")
    )

    body = _BASE_TAG_RE.sub(b"", body)  # 去掉旧 <base>，避免与注入的冲突
    if settings.proxy_strip_security_headers:
        body = _META_CSP_RE.sub(b"", body)

    def _sub_attr(match: re.Match[bytes]) -> bytes:
        url = match.group("url")
        if url.startswith(prefix_bytes) or url.startswith(b"/gw/"):
            return match.group(0)
        return match.group("pre") + match.group("q") + prefix_bare + url + match.group("q")

    def _sub_abs_attr(match: re.Match[bytes]) -> bytes:
        raw = match.group("url").decode("latin-1")
        rewritten = _rewrite_absolute_url(
            raw, origin=origin, base_path=base_path, prefix_bare=prefix_bare.decode("latin-1")
        )
        if rewritten is None:
            return match.group(0)
        return match.group("pre") + match.group("q") + rewritten.encode("latin-1") + match.group("q")

    body = _ATTR_URL_RE.sub(_sub_attr, body)
    if abs_ready:
        body = _ABS_ATTR_URL_RE.sub(_sub_abs_attr, body)
    body = _rewrite_css_urls(
        body,
        prefix_bare=prefix_bare,
        prefix_bytes=prefix_bytes,
        origin=origin,
        base_path=base_path,
        abs_ready=abs_ready,
    )

    # shim 必须排在页面里任何脚本之前，才能拦住页面自身的请求；
    # <base> 也必须尽量靠前，否则它前面的 url 属性不会被解析到前缀下。
    # 放在属性重写**之后**注入：shim 里没有 `src="/x"` 这类模式，但顺序明确更省心。
    injected = b"" if _SHIM_MARK in body else client_shim(prefix, target_url=target_url)
    injected += b'<base href="' + prefix_bytes + b'">'

    head_match = _HEAD_RE.search(body)
    if head_match:
        at = head_match.end()
        return body[:at] + injected + body[at:]
    return injected + body


def rewrite_css(body: bytes, *, prefix: str, target_url: str = "") -> bytes:
    """独立 CSS 文件里的 url(/xxx) 也要重写，否则背景图/字体在网关下会 404。"""
    if not body:
        return body
    prefix_bytes = prefix.encode("utf-8")
    prefix_bare = prefix_bytes.rstrip(b"/")
    origin, base_path = _url_origin_and_base(target_url)
    return _rewrite_css_urls(
        body,
        prefix_bare=prefix_bare,
        prefix_bytes=prefix_bytes,
        origin=origin,
        base_path=base_path,
        abs_ready=_abs_rewrite_ready(
            origin=origin, base_path=base_path, prefix_bare=prefix_bare.decode("latin-1")
        ),
    )


def _apply_raw_headers(response: Response, raw_headers: list[tuple[bytes, bytes]]) -> Response:
    """直接替换 raw_headers。

    Starlette 的 Response / StreamingResponse 都不接受 raw_headers 作为构造参数
    （它只是实例属性），而普通 headers 字典又无法承载重复的 Set-Cookie，
    因此这里构造完再整体替换。
    """
    response.raw_headers = raw_headers
    return response


def error_page(*, title: str, detail: str, hint: str = "") -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    background:linear-gradient(160deg,#f7f9fd 0%,#eef3fc 100%); color:#1b2430; padding:24px; }}
  .card {{ width:min(580px,100%); background:#fff; border:1px solid #e4ebf7; border-radius:16px;
    padding:28px 30px; box-shadow:0 12px 32px rgba(43,108,246,.10); }}
  .icon {{ width:44px; height:44px; border-radius:12px; display:flex; align-items:center;
    justify-content:center; background:linear-gradient(135deg,#ffe8e6,#ffd9d6); color:#d94a3d;
    font-size:22px; font-weight:700; }}
  h1 {{ font-size:19px; margin:16px 0 8px; }}
  p {{ font-size:14px; line-height:1.75; color:#5a6678; margin:0 0 10px; }}
  code {{ background:#f1f5fd; border:1px solid #e2eaf8; border-radius:6px; padding:2px 6px;
    font-size:12.5px; color:#2b4a7d; word-break:break-all; }}
  .hint {{ margin-top:16px; padding:12px 14px; border-radius:10px; background:#f6f9fe;
    border:1px dashed #d5e2f7; font-size:13px; line-height:1.7; color:#4a5a73; }}
  a {{ color:#2b6cf6; text-decoration:none; font-size:13.5px; }}
  .foot {{ margin-top:20px; }}
</style></head>
<body><div class="card">
  <div class="icon">!</div>
  <h1>{escape(title)}</h1>
  <p>{escape(detail)}</p>
  {f'<div class="hint">{hint}</div>' if hint else ''}
  <div class="foot"><a href="/">← 返回开发工具箱</a></div>
</div></body></html>"""
    return HTMLResponse(html, status_code=502)


# ----------------------------------------------------------------------
# 网关
# ----------------------------------------------------------------------
class ProxyGateway:
    """持有一对 httpx 客户端（校验 / 不校验 TLS），在应用生命周期内复用连接池。"""

    def __init__(self) -> None:
        self._clients: dict[bool, httpx.AsyncClient] = {}

    def _client(self, *, verify_tls: bool) -> httpx.AsyncClient:
        client = self._clients.get(verify_tls)
        if client is not None and not client.is_closed:
            return client
        timeout = httpx.Timeout(
            settings.proxy_timeout_seconds,
            connect=settings.proxy_connect_timeout_seconds,
        )
        client = httpx.AsyncClient(
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=False,
            trust_env=settings.proxy_trust_env,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
        )
        self._clients[verify_tls] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            if not client.is_closed:
                await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------------
    async def handle_http(
        self, request: Request, target: Card | CardEndpoint, path: str
    ) -> Response:
        """转发一次 HTTP 请求。

        `target` 可能是卡片，也可能是卡片下的一条环境地址（一张卡片可以挂
        多条环境地址，每条有自己的入口标识）。两者都提供 `slug` / `open_mode` /
        `target_url`（`CardEndpoint.url` 有同名只读别名），所以同一条改写流水线
        对二者完全通用 —— 这里不需要任何分支，下游的 Cookie 命名空间、路径改写、
        TLS 策略也全部跟着 `slug` 自动走。
        """
        prefix = gateway_prefix(target.slug)
        entry_path = prefix.rstrip("/")

        # 不带尾斜杠的入口统一 308 到带斜杠，保证 <base> 和相对路径生效
        if request.url.path == entry_path:
            suffix = f"?{request.url.query}" if request.url.query else ""
            return Response(status_code=308, headers={"Location": prefix + suffix})

        info = validate_target(target.target_url)
        target_url = _join_target(info.url, path, request.url.query)
        verify_tls = (
            settings.proxy_verify_tls if target.verify_tls is None else bool(target.verify_tls)
        )

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP}
        headers["X-Forwarded-For"] = _forwarded_for(request)
        headers["X-Forwarded-Proto"] = request.url.scheme
        headers["X-Forwarded-Host"] = request.headers.get("host", "")
        headers["X-Forwarded-Prefix"] = entry_path

        cookies = _filter_cookie_header(request.headers.get("cookie", ""), target.slug)
        if cookies:
            headers["Cookie"] = cookies
        else:
            headers.pop("Cookie", None)

        payload: bytes | None = None
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            payload = await request.body()
            if len(payload) > settings.proxy_max_body_bytes:
                raise AppError(
                    f"请求体超过上限 {settings.proxy_max_body_bytes // (1024 * 1024)}MB，已拒绝",
                    code="payload_too_large",
                    http_status=413,
                )

        client = self._client(verify_tls=verify_tls)
        upstream_request = client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=payload if payload else None,
        )

        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.ConnectError as exc:
            logger.warning("目标不可达 entry=%s url=%s err=%s", target.slug, target_url, exc)
            return error_page(
                title="无法连接目标内网服务",
                detail=f"跳板机尝试访问 {target_url} 失败。",
                hint=(
                    f"请依次确认：<br>"
                    f"1. 该入口配置的目标地址是否正确（当前 <code>{escape(target.target_url)}</code>）<br>"
                    f"2. 跳板机到该地址的网络与端口是否放通（安全组 / 防火墙）<br>"
                    f"3. 目标服务进程是否在运行"
                ),
            )
        except httpx.ConnectTimeout:
            return error_page(
                title="连接目标内网服务超时",
                detail=f"连接 {target_url} 超过 {settings.proxy_connect_timeout_seconds:.0f} 秒仍未建立。",
                hint="通常是 SYN 包被安全组 / 防火墙丢弃，建议在跳板机上用 <code>curl -v</code> 复测。",
            )
        except httpx.HTTPError as exc:
            logger.warning("上游请求异常 entry=%s url=%s err=%s", target.slug, target_url, exc)
            return error_page(title="请求目标服务失败", detail=f"{type(exc).__name__}: {exc}")

        content_type = upstream.headers.get("content-type", "").lower()
        rewrite_kind = ""
        if settings.proxy_rewrite_html:
            if content_type.startswith(("text/html", "application/xhtml")):
                rewrite_kind = "html"
            elif content_type.startswith("text/css"):
                rewrite_kind = "css"

        if rewrite_kind:
            return await self._rewritten_response(
                upstream=upstream,
                request=request,
                card=target,
                prefix=prefix,
                target_url=info.url,
                kind=rewrite_kind,
            )
        return self._stream_response(
            upstream=upstream, request=request, card=target, prefix=prefix, target_url=info.url
        )

    # ------------------------------------------------------------------
    def _base_raw_headers(
        self,
        upstream: httpx.Response,
        *,
        card: Card | CardEndpoint,
        prefix: str,
        request: Request,
        target_url: str,
    ) -> list[tuple[bytes, bytes]]:
        secure_ok = request.url.scheme == "https"
        raw_headers: list[tuple[bytes, bytes]] = []
        for key, value in upstream.headers.items():
            lower = key.lower()
            if lower in _RESPONSE_DROP:
                continue
            if settings.proxy_strip_security_headers and lower in _SECURITY_HEADERS_TO_STRIP:
                continue
            raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))

        location = upstream.headers.get("location")
        if location:
            rewritten = _rewrite_location(location, prefix=prefix, target_url=target_url)
            raw_headers.append((b"location", rewritten.encode("latin-1")))

        for cookie_value in upstream.headers.get_list("set-cookie"):
            rewritten = _rewrite_set_cookie(
                cookie_value, slug=card.slug, prefix=prefix, secure_ok=secure_ok
            )
            raw_headers.append((b"set-cookie", rewritten.encode("latin-1")))
        return raw_headers

    async def _rewritten_response(
        self,
        *,
        upstream: httpx.Response,
        request: Request,
        card: Card,
        prefix: str,
        target_url: str,
        kind: str = "html",
    ) -> Response:
        """HTML / CSS：整块读取 → 重写 → 返回（httpx 在此过程中自动解压）。"""
        try:
            body = await upstream.aread()
            status_code = upstream.status_code
            raw_headers = self._base_raw_headers(
                upstream, card=card, prefix=prefix, request=request, target_url=target_url
            )
        except httpx.HTTPError as exc:
            logger.warning("读取上游响应失败 card=%s err=%s", card.slug, exc)
            await upstream.aclose()
            return error_page(title="读取目标响应失败", detail=f"{type(exc).__name__}: {exc}")
        finally:
            if not upstream.is_closed:
                await upstream.aclose()

        rewritten = (
            rewrite_html(body, prefix=prefix, target_url=target_url)
            if kind == "html"
            else rewrite_css(body, prefix=prefix, target_url=target_url)
        )
        # 已解压，必须去掉 content-encoding 并按重写后的长度重算 content-length
        raw_headers = [(k, v) for k, v in raw_headers if k != b"content-encoding"]
        raw_headers = [(k, v) for k, v in raw_headers if k != b"content-length"]
        raw_headers.append((b"content-length", str(len(rewritten)).encode("latin-1")))

        response = Response(status_code=status_code)
        if status_code not in (204, 304):
            response.body = rewritten
        return _apply_raw_headers(response, raw_headers)

    def _stream_response(
        self,
        *,
        upstream: httpx.Response,
        request: Request,
        card: Card,
        prefix: str,
        target_url: str,
    ) -> Response:
        """非 HTML：原样流式透传，保留 content-encoding，不做内存缓冲。"""
        raw_headers = self._base_raw_headers(
            upstream, card=card, prefix=prefix, request=request, target_url=target_url
        )
        status_code = upstream.status_code

        async def body_iterator() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError as exc:  # 传输中途断开
                logger.warning("上游传输中断 card=%s err=%s", card.slug, exc)
            finally:
                await upstream.aclose()

        return _apply_raw_headers(
            StreamingResponse(body_iterator(), status_code=status_code), raw_headers
        )

    # ------------------------------------------------------------------
    async def handle_websocket(self, websocket, card: Card | CardEndpoint, path: str) -> None:  # noqa: ANN001
        from app.services.ws_proxy import relay_websocket

        await relay_websocket(websocket, card=card, path=path)


gateway = ProxyGateway()


__all__ = [
    "ProxyGateway",
    "gateway",
    "gateway_prefix",
    "gateway_slug_from_referer",
    "build_launch_url",
    "client_shim",
    "is_portal_owned_path",
    "rewrite_html",
    "rewrite_css",
    "error_page",
]