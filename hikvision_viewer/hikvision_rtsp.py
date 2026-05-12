"""Compose and parse Hikvision-style RTSP URLs (no Qt; used by config editor and loader).

Paths look like ``.../Streaming/Channels/<expr>``: numeric ids (101/102, NVR channels), or
placeholder text (e.g. ``{CAM}01``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
import logging
from urllib.parse import unquote, urlparse, urlunparse

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


@dataclass(frozen=True)
class RtspHikEndpointHints:
    """Best-effort values from ``rtsp://user:pass@host:port[/…]`` plus channel path suffix."""

    user: str
    password_expr: str
    host_expr: str
    port: int
    #: Text after ``/Streaming/Channels/`` (digits, ``{CAM}01``, etc.) or ``None``.
    channel_suffix: str | None


def extract_rtsp_hik_endpoint_hints(url: str) -> RtspHikEndpointHints | None:
    u = urlparse(url.strip())
    if u.scheme != "rtsp":
        return None
    parsed = _parse_rtsp_netloc(u.netloc)
    if parsed is None:
        return None
    usr, passwd, host, port = parsed
    path_nr = ((u.path or "").replace("\\", "/")).rstrip("/")
    ch_suffix: str | None = None
    mp = re.search(r"/Streaming/Channels/([^/?#]+)$", path_nr)
    if mp:
        ch_suffix = mp.group(1).strip()
        if not ch_suffix:
            ch_suffix = None
    return RtspHikEndpointHints(
        user=usr,
        password_expr=passwd,
        host_expr=host,
        port=port,
        channel_suffix=ch_suffix,
    )


def merge_rtsp_netloc_into_url(
    url: str,
    user: str,
    password_expr: str,
    host_expr: str,
    port: str | int,
) -> str:
    """Replace scheme netloc parts; preserve path/query/fragment (NVR placeholders in path unchanged)."""
    p = urlparse(url.strip())
    if p.scheme != "rtsp":
        return url.strip()
    host = host_expr.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    uu = user.strip() or "admin"
    port_s = str(port).strip()
    if not port_s:
        port_s = "554"
    netloc = f"{uu}:{password_expr}@{host}:{port_s}"
    return urlunparse(
        (
            p.scheme,
            netloc,
            p.path or "",
            p.params or "",
            p.query or "",
            p.fragment or "",
        )
    )


def merge_channel_segment_in_hik_path(url: str, channel_expr: str) -> str:
    """Replace the path suffix after ``/Streaming/Channels/`` (digits or placeholders)."""
    s = url.strip()
    ch = channel_expr.strip()
    if not ch:
        return s
    p = urlparse(s)
    if p.scheme != "rtsp":
        return s
    path_nr = ((p.path or "").replace("\\", "/")).rstrip("/")
    head, sep, _old = path_nr.rpartition("/Streaming/Channels/")
    if not sep:
        return s
    new_path = f"{head}{sep}{ch}"
    return urlunparse(
        (
            p.scheme,
            p.netloc,
            new_path,
            p.params or "",
            p.query or "",
            p.fragment or "",
        )
    )


def build_hikvision_rtsp_url(
    user: str,
    password_expr: str,
    host_expr: str,
    *,
    port: str | int = 554,
    channel: str | int = 101,
) -> str:
    u = user.strip() or "admin"
    host = host_expr.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port_s = str(port).strip() or "554"
    ch_s = str(channel).strip() or "101"
    return f"rtsp://{u}:{password_expr}@{host}:{port_s}/Streaming/Channels/{ch_s}"


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
    if ch < 1 or ch > 999999:
        LOG.debug("Unsupported Hikvision channel in URL (out of range): %s", ch)
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
