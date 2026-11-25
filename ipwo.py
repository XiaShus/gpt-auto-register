"""IPWO 动态住宅代理（账密网关，与 ZooProxy 网关类似）。"""
from __future__ import annotations

import logging
import re
import secrets
import string
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse

from zooproxy import probe_https_proxy_detail

_SID_TIME_RE = re.compile(r"_sid_[^_]+_time_\d+", re.I)
_SESSION_RE = re.compile(r"_session_[^:_]+", re.I)

logger = logging.getLogger(__name__)


class IpwoError(RuntimeError):
    pass


def _new_sid(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(4, length)))


def parse_ipwo_line(line: str) -> tuple[str, str, str]:
    raw = (line or "").strip()
    if not raw:
        raise IpwoError("IPWO 配置行为空")
    parts = raw.split(":")
    if len(parts) < 3:
        raise IpwoError("IPWO 配置须为 host:port:username 或 host:port:username:password")
    host = f"{parts[0]}:{parts[1]}"
    user = parts[2]
    pwd = parts[3] if len(parts) > 3 else ""
    return host, user, pwd


def build_ipwo_proxy_url(
    *,
    host: str,
    username_base: str,
    password: str,
    zone: str = "US",
    session_minutes: int = 10,
    session_id: Optional[str] = None,
    scheme: str = "socks5h",
    rotate_session: bool = True,
) -> str:
    h = (host or "").strip()
    if h.startswith("http://") or h.startswith("https://"):
        h = urlparse(h).netloc or h
    if not h or ":" not in h:
        raise IpwoError("IPWO host 须为 hostname:port，如 us.ipwo.net:7878")
    base = (username_base or "").strip()
    pwd = password or ""
    if not base:
        raise IpwoError("IPWO 缺少用户名")
    z = (zone or "US").strip().upper()
    if session_id:
        sid = session_id.strip()
    elif rotate_session:
        sid = _new_sid()
    else:
        m = _SID_TIME_RE.search(base)
        sid = m.group(0).split("_")[2] if m else _new_sid()
    try:
        t_min = max(1, int(session_minutes))
    except (TypeError, ValueError):
        t_min = 10
    if re.search(r"_custom_zone_[A-Za-z0-9]+", base, flags=re.I):
        user = re.sub(
            r"_custom_zone_[A-Za-z0-9]+",
            f"_custom_zone_{z}",
            base,
            count=1,
            flags=re.I,
        )
    elif "_zone_" in base:
        user = base
    else:
        user = f"{base}_custom_zone_{z}"
    if _SID_TIME_RE.search(user):
        user = _SID_TIME_RE.sub(f"_sid_{sid}_time_{t_min}", user, count=1)
    elif _SESSION_RE.search(user):
        user = _SESSION_RE.sub(f"_session_{sid}", user, count=1)
    elif "_sid_" not in user and "_session_" not in user:
        user = f"{user}_sid_{sid}_time_{t_min}"
    sch = (scheme or "socks5h").strip().lower()
    if sch == "socks5":
        sch = "socks5h"
    user_q = quote(user, safe="")
    pwd_q = quote(pwd, safe="")
    proxy = f"{sch}://{user_q}:{pwd_q}@{h}"
    logger.info("IPWO 代理: %s:***@%s zone=%s sid=%s", base[:20], h, z, sid)
    return proxy


def resolve_ipwo_proxy(options: Mapping[str, Any]) -> str:
    line = (options.get("ipwo_line") or "").strip()
    if line:
        host, user, pwd = parse_ipwo_line(line)
    else:
        host = options.get("ipwo_host") or "us.ipwo.net:7878"
        user = options.get("ipwo_username") or ""
        pwd = options.get("ipwo_password") or ""
    try:
        max_probe = max(1, int(options.get("ipwo_probe_attempts") or 4))
    except (TypeError, ValueError):
        max_probe = 4
    zone = options.get("ipwo_zone") or "NA"
    session_minutes = options.get("ipwo_session_minutes") or 10
    scheme = options.get("ipwo_scheme") or "http"
    last_err = ""
    for attempt in range(max_probe):
        proxy = build_ipwo_proxy_url(
            host=host,
            username_base=user,
            password=pwd,
            zone=zone,
            session_minutes=session_minutes,
            scheme=scheme,
            rotate_session=True,
        )
        ok, probe_err = probe_https_proxy_detail(proxy)
        if ok:
            if attempt > 0:
                logger.info("IPWO 预检通过（第 %d 次 sid）", attempt + 1)
            return proxy
        last_err = probe_err or f"预检未通过 zone={zone}"
        logger.warning("IPWO 预检失败，换 sid (%d/%d): %s", attempt + 1, max_probe, last_err[:160])
    raise IpwoError(
        f"IPWO 连续 {max_probe} 次预检失败（{last_err}），请检查账密、余额或 zone"
    )
