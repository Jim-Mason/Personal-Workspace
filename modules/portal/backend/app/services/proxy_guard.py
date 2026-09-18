"""内网目标地址的安全校验（SSRF 防护）。

跳板机场景下我们**确实**需要代理到私有地址，所以这里的重点不是"禁止内网"，
而是：
1. 只允许 http/https，禁止 file://、gopher:// 等危险协议
2. 永远禁止云元数据地址（169.254.169.254 / fd00:ec2::254 等），避免凭据泄露
3. 可选的目标白名单
4. 禁止 URL 中携带用户名密码（避免把凭据写进配置和日志）
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.errors import BadRequestError, ForbiddenError

logger = logging.getLogger("app.proxy.guard")

_ALLOWED_SCHEMES = {"http", "https"}

# 任何情况下都禁止访问的网段 / 地址
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),      # 链路本地，含云元数据 169.254.169.254
    ipaddress.ip_network("fe80::/10"),           # IPv6 链路本地
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.100.100.200/32"),  # 阿里云元数据
    ipaddress.ip_network("fd00:ec2::254/128"),   # AWS IPv6 元数据
    ipaddress.ip_network("224.0.0.0/4"),         # 组播
    ipaddress.ip_network("255.255.255.255/32"),
]

_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_TTL_SECONDS = 60.0


class TargetInfo:
    __slots__ = ("url", "scheme", "host", "port", "path", "query")

    def __init__(self, *, url: str, scheme: str, host: str, port: int, path: str, query: str) -> None:
        self.url = url
        self.scheme = scheme
        self.host = host
        self.port = port
        self.path = path
        self.query = query

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TargetInfo {self.scheme}://{self.host}:{self.port}{self.path}>"


def _resolve(host: str) -> list[str]:
    """带 TTL 缓存的 DNS 解析，避免每个请求都查一次。"""
    now = time.monotonic()
    cached = _DNS_CACHE.get(host)
    if cached and now - cached[0] < _DNS_TTL_SECONDS:
        return cached[1]

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []

    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addresses:
            addresses.append(addr)
    _DNS_CACHE[host] = (now, addresses)
    return addresses


def _is_blocked(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return any(ip in network for network in _BLOCKED_NETWORKS)


def _host_allowed(host: str) -> bool:
    allowlist = settings.proxy_allowed_hosts
    if not allowlist:
        return True
    host_lower = host.lower()
    for pattern in allowlist:
        pattern_lower = pattern.lower().strip()
        if not pattern_lower:
            continue
        if pattern_lower.startswith("*."):
            suffix = pattern_lower[1:]  # ".example.com"
            if host_lower.endswith(suffix) or host_lower == pattern_lower[2:]:
                return True
        elif host_lower == pattern_lower:
            return True
    return False


def validate_target(raw_url: str, *, strict_dns: bool = True) -> TargetInfo:
    """解析并校验目标地址；不合法时抛 AppError。

    strict_dns=False 用于"保存配置"场景：此时域名可能还没解析好，
    不做解析可行性拦截，但协议、凭据、字面量 IP 的禁用网段、白名单仍然强制校验。
    """
    url = (raw_url or "").strip()
    if not url:
        raise BadRequestError("目标地址未配置")

    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise BadRequestError(f"不支持的目标协议：{parts.scheme or '(空)'}，仅允许 http / https")
    if parts.username or parts.password:
        raise ForbiddenError("目标地址中不允许携带用户名密码，请改用独立的认证方式")

    host = parts.hostname
    if not host:
        raise BadRequestError("目标地址缺少主机名")

    if not _host_allowed(host):
        raise ForbiddenError(f"目标主机 {host} 不在允许的白名单内")

    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)

    # 主机名本身是 IP 时直接判断
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        addresses = [str(literal_ip)]
    else:
        addresses = _resolve(host)
        if not addresses and strict_dns:
            raise BadRequestError(f"无法解析目标主机：{host}")

    for address in addresses:
        if _is_blocked(address):
            raise ForbiddenError(f"目标主机 {host} 解析到受限地址 {address}，已拒绝访问")
        if not settings.proxy_allow_private_targets:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ForbiddenError(
                    f"目标主机 {host} 指向内网地址 {address}，"
                    "当前配置禁止代理到内网（PROXY_ALLOW_PRIVATE_TARGETS=false）"
                )

    return TargetInfo(
        url=url,
        scheme=parts.scheme.lower(),
        host=host,
        port=port,
        path=parts.path or "/",
        query=parts.query,
    )


def clear_dns_cache() -> None:
    _DNS_CACHE.clear()


__all__ = ["validate_target", "TargetInfo", "clear_dns_cache"]
