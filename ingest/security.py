"""抓取前的地址校验（SSRF / 危险重定向防护）。

工具入口与聊天入口的 URL 都可能由模型或用户提供，一律在这里过一遍：
协议白名单 → 主机形态 → 字面 IP 分类。DNS 解析不在这里做（会阻塞），
调用方必须在每一跳重定向后重新校验。
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

FORBIDDEN_HOST_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".internal",
    ".localhost",
    ".home.arpa",
    ".lan",
)

FORBIDDEN_HOSTS: frozenset[str] = frozenset(
    {"localhost", "metadata", "metadata.google.internal", "instance-data"}
)

# 127.1 / 2130706433 / 0x7f000001 / 0177.0.0.1 这类写法：ipaddress 认不出，
# 但部分平台的 getaddrinfo 会把它们解析成内网地址，一律拒绝。
_NUMERIC_HOST = re.compile(
    r"^(?:[0-9]+|0x[0-9a-f]+)(?:\.(?:[0-9]+|0x[0-9a-f]+))*$", re.IGNORECASE
)


def _host_of(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return (parts.hostname or "").strip().lower()


def is_forbidden_host(host: str) -> bool:
    """主机名或字面 IP 是否属于内网 / 回环 / 链路本地 / 保留地址。"""
    name = (host or "").strip().strip("[]").lower()
    if not name:
        return True
    if name in FORBIDDEN_HOSTS:
        return True
    if any(name.endswith(suffix) for suffix in FORBIDDEN_HOST_SUFFIXES):
        return True
    if _NUMERIC_HOST.fullmatch(name):
        # 纯数字/十六进制形态：不是标准点分十进制，也不可能是正常域名。
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def check_url(url: str) -> tuple[bool, str]:
    """返回 (是否可抓, 原因码)。原因码用于日志与工具返回值。"""
    text = (url or "").strip()
    if not text:
        return False, "url_empty"
    try:
        parts = urlsplit(text)
    except ValueError:
        return False, "url_unparsable"
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, "url_scheme_denied"
    host = _host_of(text)
    if not host:
        return False, "url_without_host"
    if is_forbidden_host(host):
        return False, "url_host_denied"
    return True, "url_ok"


__all__ = ["ALLOWED_SCHEMES", "check_url", "is_forbidden_host"]
