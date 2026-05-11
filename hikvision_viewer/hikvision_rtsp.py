"""Compose and parse Hikvision-style RTSP URLs (no Qt; used by the config editor)."""

from __future__ import annotations

import re
from dataclasses import dataclass
import logging
from urllib.parse import unquote, urlparse

LOG = logging.getLogger(__name__)


def _host_and_port(hostport: str) -> tuple[str, int]:
    """Host and port from netloc host part; preserves host string case (unlike urlparse.hostname)."""
    hostport = hostport.strip()
    if not hostport:
        return "", 554
    if hostport.startswith("["):
        end = hostport.find("]")
        if end == -1:
            return hostport, 554
        host = hostport[1:end]
        rest = hostport[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, 554
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        if port_s.isdigit():
            return host, int(port_s)
    return hostport, 554


def _parse_rtsp_netloc(netloc: str) -> tuple[str, str, str, int] | None:
    """user, password, host, port from rtsp netloc; host casing preserved for {PLACEHOLDER}s."""
    netloc = netloc.strip()
    if not netloc:
        return None
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
    else:
        userinfo, hostport = "", netloc
    if ":" in userinfo:
        idx = userinfo.index(":")
        raw_user, raw_password = userinfo[:idx], userinfo[idx + 1 :]
    else:
        raw_user, raw_password = userinfo, ""
    user = unquote(raw_user) or "admin"
    password = unquote(raw_password)
    host, port = _host_and_port(hostport)
    if not host:
        return None
    return user, password, host, port


@dataclass(frozen=True)
class HikvisionUrlParts:
    user: str
    password_expr: str
    host_expr: str
    port: int
    channel: int


def build_hikvision_rtsp_url(
    user: str,
    password_expr: str,
    host_expr: str,
    *,
    port: int = 554,
    channel: int = 101,
) -> str:
    u = user.strip() or "admin"
    host = host_expr.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return (
        f"rtsp://{u}:{password_expr}@{host}:{port}/Streaming/Channels/{channel}"
    )


def try_parse_hikvision_rtsp_url(url: str) -> HikvisionUrlParts | None:
    u = urlparse(url.strip())
    if u.scheme != "rtsp":
        LOG.debug("URL is not RTSP, cannot parse as Hikvision: %s", url)
        return None
    path = (u.path or "").replace("\\", "/").rstrip("/")
    m = re.search(r"/Streaming/Channels/(\d+)$", path)
    if not m:
        LOG.debug("URL path does not match Hikvision channels path: %s", u.path)
        return None
    ch = int(m.group(1))
    if ch not in (101, 102):
        LOG.debug("Unsupported Hikvision channel in URL: %s", ch)
        return None
    parsed = _parse_rtsp_netloc(u.netloc)
    if parsed is None:
        LOG.debug("Could not parse RTSP netloc: %s", u.netloc)
        return None
    user, password, host, port = parsed
    return HikvisionUrlParts(
        user=user,
        password_expr=password,
        host_expr=host,
        port=port,
        channel=ch,
    )
