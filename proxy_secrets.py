"""VPS 本地 .bench-secrets.env + 矩阵最优默认，供 WebUI 自动合并（勿提交密钥文件）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SECRETS_PATH = ROOT / ".bench-secrets.env"

# 与 register-matrix-20261012 一致；UK 已在 proxy_pool 排除
OPTIMAL_PROXY: dict[str, Any] = {
    "zooproxy_enabled": True,
    "ipwo_enabled": True,
    "zooproxy_mode": "gateway",
    "zooproxy_gateway_host": "us-eu.zooproxy.com:5000",
    "zooproxy_gateway_session_minutes": 10,
    "zooproxy_probe_attempts": 6,
    "ipwo_host": "us.ipwo.net:7878",
    "ipwo_scheme": "http",
    "ipwo_session_minutes": 10,
    "ipwo_probe_attempts": 6,
    "ipwo_zone": "NA",
    "proxy_multi_country": True,
    "proxy_countries_zoo": "FR,JP,BR,CA,DE,NA,US",
    "proxy_countries_ipwo": "BR,DE,NA,FR,JP,US",
    "proxy_dual_mode": "split_workers",
    "proxy_balance_fallback_attempts": 6,
    # 每格（厂商×国家）在途 run 上限。13 个格子 × 5 = 65 个并发槽，
    # 支撑 concurrency=44 而不出现「全格满负载→退化兜底」。
    "proxy_max_in_flight_per_cell": 5,
    "auto_resume_on_network": True,
    "auto_resume_delay_seconds": 90.0,
    "zooproxy_fail_sleep": 15.0,
    "cool_down_seconds": 5.0,
    "concurrency": 44,
    "otp_timeout": 180,
    "mail_otp_per_domain": 4,
    "otp_circuit_break_threshold": 8,
    "otp_circuit_resume_seconds": 120.0,
}


def _load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    if not SECRETS_PATH.is_file():
        return out
    for line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _ipwo_username_base(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if "_custom_zone_" in s.lower():
        return re.split(r"_custom_zone_", s, maxsplit=1, flags=re.I)[0]
    return s


def apply_optimal_proxy_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """请求选项 + 最优默认 + 本地 secrets（空密码/账号时用服务器侧配置）。"""
    out: dict[str, Any] = {**OPTIMAL_PROXY, **(options or {})}
    sec = _load_secrets()

    zoo_acct = (sec.get("ZOOPROXY_GATEWAY_ACCOUNT") or out.get("zooproxy_gateway_account") or "").strip()
    zoo_pwd = sec.get("ZOOPROXY_GATEWAY_PASSWORD")
    if zoo_acct:
        out["zooproxy_gateway_account"] = zoo_acct
    if zoo_pwd is not None and not (out.get("zooproxy_gateway_password") or "").strip():
        out["zooproxy_gateway_password"] = zoo_pwd
    if (out.get("zooproxy_gateway_password") or "").strip():
        out["zooproxy_enabled"] = True

    ipwo_raw = (sec.get("IPWO_USERNAME") or out.get("ipwo_username") or "").strip()
    ipwo_base = _ipwo_username_base(ipwo_raw)
    if ipwo_base:
        out["ipwo_username"] = ipwo_base
    ipwo_pwd = sec.get("IPWO_PASSWORD")
    if ipwo_pwd is not None and not (out.get("ipwo_password") or "").strip():
        out["ipwo_password"] = ipwo_pwd
    if (out.get("ipwo_username") or "").strip():
        out["ipwo_enabled"] = True

    if sec.get("IPWO_HOST"):
        out["ipwo_host"] = sec["IPWO_HOST"]
    if sec.get("ZOOPROXY_GATEWAY_HOST"):
        out["zooproxy_gateway_host"] = sec["ZOOPROXY_GATEWAY_HOST"]

    return out


_AUTO_CLIENT_KEEP = (
    "target_count",
    "want_2fa",
    "want_access_token",
    "want_session_token",
    "want_refresh_token",
    "proxy_pool",
    "proxy",
    "allow_existing_login",
    # 并发与 OTP 超时由调用方（WebUI 表单/预设）决定，不被下面的
    # OPTIMAL_PROXY 强制覆盖 —— 否则界面里改并发是静默无效的。
    # proxy_max_in_flight_per_cell 同理：调并发时要能同时调它，
    # 否则并发一涨就撞上「全格满负载」的退化兜底。为 0/缺省时用预设值。
    "concurrency",
    "otp_timeout",
    "proxy_max_in_flight_per_cell",
)

# 这些键在请求里为 0 时表示「没填」，应当回落到预设值而不是真的设成 0。
_ZERO_MEANS_UNSET = frozenset({"proxy_max_in_flight_per_cell"})


def apply_for_auto_loop(client: dict[str, Any] | None) -> dict[str, Any]:
    """批量跑号：矩阵最优 + secrets；跑号参数以最优为准（忽略浏览器里旧的并发/空密码）。"""
    c = client or {}
    out = apply_optimal_proxy_options(c)
    for k, v in OPTIMAL_PROXY.items():
        out[k] = v
    for k in _AUTO_CLIENT_KEEP:
        v = c.get(k)
        if v is None:
            continue
        # 这几个键 0 = 「未指定」，要用预设值；否则一旦请求没带
        # proxy_max_in_flight_per_cell，就会被 0 覆盖成 _max_in_flight 的
        # 兜底上限 2，把并发死死卡在 26。
        if k in _ZERO_MEANS_UNSET and not v:
            continue
        out[k] = v
    return apply_optimal_proxy_options(out)


def preset_for_frontend() -> dict[str, Any]:
    """camelCase，供 Pinia 表单；含密码（仅私有 WebUI）。"""
    o = apply_optimal_proxy_options({})
    return {
        "zooproxyEnabled": bool(o.get("zooproxy_enabled")),
        "zooproxyMode": o.get("zooproxy_mode") or "gateway",
        "zooproxyGatewayHost": o.get("zooproxy_gateway_host") or "",
        "zooproxyGatewayAccount": o.get("zooproxy_gateway_account") or "",
        "zooproxyGatewayPassword": o.get("zooproxy_gateway_password") or "",
        "zooproxyGatewayRegion": o.get("zooproxy_gateway_region") or "US",
        "zooproxyGatewaySessionMinutes": int(o.get("zooproxy_gateway_session_minutes") or 10),
        "ipwoEnabled": bool(o.get("ipwo_enabled")),
        "ipwoHost": o.get("ipwo_host") or "",
        "ipwoUsername": o.get("ipwo_username") or "",
        "ipwoPassword": o.get("ipwo_password") or "",
        "ipwoZone": o.get("ipwo_zone") or "NA",
        "ipwoScheme": o.get("ipwo_scheme") or "http",
        "ipwoSessionMinutes": int(o.get("ipwo_session_minutes") or 10),
        "proxyMultiCountry": bool(o.get("proxy_multi_country", True)),
        "proxyCountriesZoo": o.get("proxy_countries_zoo") or "",
        "proxyCountriesIpwo": o.get("proxy_countries_ipwo") or "",
        "proxyDualMode": o.get("proxy_dual_mode") or "split_workers",
        "autoConcurrency": int(o.get("concurrency") or 24),
        "autoCoolDown": float(o.get("cool_down_seconds") or 5),
        "otpTimeout": int(o.get("otp_timeout") or 180),
        "autoResumeOnNetwork": bool(o.get("auto_resume_on_network", True)),
    }
