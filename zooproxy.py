"""ZooProxy 动态住宅 IP：账号网关 SOCKS5（推荐）或白名单 API。

网关（控制台「动态住宅流量」）示例::

    curl --socks5 us-eu.zooproxy.com:5000 -U "账号-region-US-sid-xxx-t-10:密码" ipinfo.io

每个号换 ``sid`` 即换出口 IP。注册机生成 ``socks5h://user:pass@host:port``。

白名单 API 返回 ``host:port`` 多为 HTTP 代理，访问 HTTPS 易 TLS35，仅作备用。
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import string
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

_WHITELIST_RE = re.compile(r"not added to whitelist", re.I)


class ZooProxyError(RuntimeError):
    pass


def _normalize_scheme(scheme: str) -> str:
    s = (scheme or "http").strip().lower()
    if s in ("socks5", "socks5h"):
        return "socks5h"
    if s in ("http", "https"):
        return s
    return "http"


def _proxy_url(host: str, port: str | int, scheme: str) -> str:
    h = (host or "").strip()
    p = str(port or "").strip()
    if not h or not p:
        raise ZooProxyError("ZooProxy 返回缺少 host/port")
    sch = _normalize_scheme(scheme)
    return f"{sch}://{h}:{p}"


def parse_zooproxy_body(text: str, scheme: str = "http") -> str:
    """解析 API 响应体为 curl_cffi 可用的代理 URL。"""
    raw = (text or "").strip()
    if not raw:
        raise ZooProxyError("ZooProxy 响应为空")
    if _WHITELIST_RE.search(raw):
        raise ZooProxyError(
            "ZooProxy 白名单未通过：请把 VPS 出口 IP 加到 ZooProxy 白名单后再试"
        )
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ZooProxyError(f"ZooProxy JSON 解析失败: {e}") from e
        if isinstance(data, list) and data:
            item = data[0]
            if isinstance(item, dict):
                return _proxy_url(
                    str(item.get("host") or item.get("ip") or ""),
                    str(item.get("port") or ""),
                    scheme,
                )
        if isinstance(data, dict):
            return _proxy_url(
                str(data.get("host") or data.get("ip") or ""),
                str(data.get("port") or ""),
                scheme,
            )
        raise ZooProxyError(f"ZooProxy JSON 结构无法识别: {raw[:200]}")
    # txt：host:port 或 ip:port
    line = raw.splitlines()[0].strip()
    if ":" in line:
        host, _, port = line.partition(":")
        return _proxy_url(host, port, scheme)
    raise ZooProxyError(f"ZooProxy 响应无法解析: {raw[:200]}")


def fetch_zooproxy_url(
    api_url: str,
    *,
    scheme: str = "http",
    timeout: float = 20.0,
    session: Optional[Any] = None,
) -> str:
    """GET 提取链接，返回单个代理 URL（每个注册任务调用一次即可换 IP）。"""
    url = (api_url or "").strip()
    if not url:
        raise ZooProxyError("未配置 ZooProxy API URL")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ZooProxyError("ZooProxy API 必须是 http(s) URL")

    http = session if session is not None else requests
    try:
        resp = http.get(url, timeout=timeout)
    except Exception as e:
        raise ZooProxyError(f"请求 ZooProxy API 失败: {e}") from e
    body = (resp.text or "").strip()
    if resp.status_code != 200:
        raise ZooProxyError(
            f"ZooProxy HTTP {resp.status_code}: {body[:220] or resp.reason}"
        )
    proxy = parse_zooproxy_body(body, scheme=scheme)
    logger.info("ZooProxy 已提取代理: %s (api=%s)", proxy, url.split("?", 1)[0])
    return proxy


def _new_session_id(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(4, length)))


def build_gateway_socks5_url(
    *,
    host: str,
    account: str,
    password: str,
    region: str = "US",
    session_minutes: int = 10,
    session_id: Optional[str] = None,
) -> str:
    """构造 ZooProxy 账号网关 SOCKS5 URL（每号新 sid → 新 IP）。"""
    h = (host or "").strip()
    if h.startswith("http://") or h.startswith("https://"):
        h = urlparse(h).netloc or h
    if not h or ":" not in h:
        raise ZooProxyError("网关 host 须为 hostname:port，如 us-eu.zooproxy.com:5000")
    acct = (account or "").strip()
    pwd = password or ""
    if not acct or not pwd:
        raise ZooProxyError("ZooProxy 网关缺少账号或密码")
    reg = (region or "US").strip().upper()
    sid = (session_id or _new_session_id()).strip()
    try:
        t_min = max(1, int(session_minutes))
    except (TypeError, ValueError):
        t_min = 10
    user = f"{acct}-region-{reg}-sid-{sid}-t-{t_min}"
    user_q = quote(user, safe="")
    pwd_q = quote(pwd, safe="")
    proxy = f"socks5h://{user_q}:{pwd_q}@{h}"
    logger.info(
        "ZooProxy 网关 SOCKS5: %s:***@%s (region=%s sid=%s)",
        acct,
        h,
        reg,
        sid,
    )
    return proxy


def _probe_navigation_headers_chrome() -> tuple[str, dict]:
    """与 auth_flow.warmup 对齐：Chrome + client hints，否则 CF 403 只有 __cf_bm。"""
    from fingerprint import _CHROME_VERSIONS, ua_for_impersonate

    c = _CHROME_VERSIONS[0]
    imp = c["impersonate"]
    ua = ua_for_impersonate(imp, "")
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "priority": "u=0, i",
        "User-Agent": ua,
        "sec-ch-ua": c["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-full-version-list": c["sec_ch_ua_full_version_list"],
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform-version": '"10.0.19045"',
    }
    return imp, headers


def _scrub_probe_err(msg: str) -> str:
    s = str(msg or "")
    s = re.sub(r"://([^/@\s]+):([^@/\s]+)@", "://<user>:<pass>@", s)
    return s[:240]


def probe_https_proxy_detail(proxy_url: str, timeout: float = 28.0) -> tuple[bool, str]:
    """预检 chatgpt.com 是否种到 oai-did。失败时返回脱敏后的原因，供上游写进 err。"""
    try:
        from curl_cffi.requests import Session as CffiSession
    except ImportError:
        return True, ""
    p = (proxy_url or "").strip()
    if not p:
        return False, "proxy_url 为空"
    if p.startswith("socks5://"):
        p = "socks5h://" + p[len("socks5://") :]
    try:
        imp, headers = _probe_navigation_headers_chrome()
        s = CffiSession(impersonate=imp)
        s.trust_env = False
        s.proxies = {"https": p, "http": p}
        resp = s.get("https://chatgpt.com", headers=headers, timeout=timeout)
        cookies = s.cookies.get_dict() if hasattr(s.cookies, "get_dict") else {}
        if "oai-did" in cookies:
            return True, ""
        err = f"HTTP {resp.status_code} 无 oai-did"
        logger.warning("代理预检失败: %s", err)
        return False, err
    except Exception as e:
        err = _scrub_probe_err(f"{type(e).__name__}: {e}")
        logger.warning("代理预检失败: %s", err)
        return False, err


def probe_https_proxy(proxy_url: str, timeout: float = 28.0) -> bool:
    """开跑前预检：必须种到 oai-did（与 warmup 同一判据），避免假通过。"""
    ok, _err = probe_https_proxy_detail(proxy_url, timeout=timeout)
    return ok


def resolve_zooproxy_proxy(
    options: Mapping[str, Any],
    *,
    session: Optional[Any] = None,
) -> str:
    """按 WebUI 选项返回本 run 使用的代理 URL（网关模式带 HTTPS 预检 + 换 sid）。"""
    mode = (options.get("zooproxy_mode") or "gateway").strip().lower()
    try:
        max_probe = max(1, int(options.get("zooproxy_probe_attempts") or 4))
    except (TypeError, ValueError):
        max_probe = 4

    if mode in ("gateway", "socks5", "account"):
        host = options.get("zooproxy_gateway_host") or "us-eu.zooproxy.com:5000"
        account = options.get("zooproxy_gateway_account") or ""
        password = options.get("zooproxy_gateway_password") or ""
        region = options.get("zooproxy_gateway_region") or "US"
        session_minutes = options.get("zooproxy_gateway_session_minutes") or 10
        last_err = ""
        for attempt in range(max_probe):
            proxy = build_gateway_socks5_url(
                host=host,
                account=account,
                password=password,
                region=region,
                session_minutes=session_minutes,
            )
            ok, probe_err = probe_https_proxy_detail(proxy)
            if ok:
                if attempt > 0:
                    logger.info("ZooProxy 预检通过（第 %d 次 sid）", attempt + 1)
                return proxy
            last_err = probe_err or f"预检未通过 @{host.split(':')[0]}"
            logger.warning(
                "ZooProxy 网关预检失败，换 sid (%d/%d): %s",
                attempt + 1,
                max_probe,
                last_err[:160],
            )
        raise ZooProxyError(
            f"ZooProxy 连续 {max_probe} 次预检失败（{last_err}），"
            "多为住宅 IP 不可用、SOCKS5 鉴权失败或欠费"
        )

    api = (options.get("zooproxy_api_url") or "").strip()
    scheme = (options.get("zooproxy_scheme") or "http").strip()
    proxy = fetch_zooproxy_url(api, scheme=scheme, session=session)
    ok, probe_err = probe_https_proxy_detail(proxy)
    if not ok:
        raise ZooProxyError(
            f"白名单 API 代理预检失败（{probe_err or 'HTTPS TLS35 常见'}），请改用账号网关 SOCKS5"
        )
    return proxy
