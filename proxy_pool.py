"""矩阵 + 在线负载均衡驱动的多国家动态代理（register-matrix 20261012-190401）。"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from proxy_balance import (
    CellKey,
    get_balancer,
    parse_proxy_label,
    report_proxy_run_outcome,
)

logger = logging.getLogger(__name__)

EXCLUDED_COUNTRIES = frozenset({"UK"})

ZOO_MATRIX_POOL: list[str] = [
    "FR", "FR", "FR",
    "JP", "JP",
    "BR", "BR",
    "CA", "CA",
    "DE", "NA",
]
IPWO_MATRIX_POOL: list[str] = [
    "BR", "BR", "BR",
    "DE", "DE",
    "NA", "NA",
    "FR", "JP",
]

DEFAULT_ZOO_COUNTRIES_CSV = "FR,JP,BR,CA,DE,NA"
DEFAULT_IPWO_COUNTRIES_CSV = "BR,DE,NA,FR,JP"


class DynamicProxyError(RuntimeError):
    pass


def parse_country_list(text: str, *, default_pool: list[str]) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return list(default_pool)
    out: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        cc = part.strip().upper()
        if cc and cc not in EXCLUDED_COUNTRIES and cc not in out:
            out.append(cc)
    return out if out else list(default_pool)


def country_for_slot(pool: list[str], slot: int) -> str:
    if not pool:
        return "US"
    return pool[slot % len(pool)]


def run_slot_index(*, worker_id: int, registered_ok: int, registered_fail: int) -> int:
    return int(worker_id) + int(registered_ok) + int(registered_fail)


def dynamic_proxy_enabled(options: Mapping[str, Any]) -> bool:
    return bool(options.get("zooproxy_enabled")) or bool(options.get("ipwo_enabled"))


def reset_proxy_balance_session() -> None:
    get_balancer().reset_session()


def _resolve_cell(options: Mapping[str, Any], cell: CellKey) -> tuple[str, str]:
    provider, region = cell
    opts = dict(options)
    if provider == "zooproxy":
        from zooproxy import ZooProxyError, resolve_zooproxy_proxy

        opts["zooproxy_gateway_region"] = region
        try:
            url = resolve_zooproxy_proxy(opts)
        except ZooProxyError as e:
            raise DynamicProxyError(str(e)) from e
        return url, f"zooproxy/{region}"

    from ipwo import IpwoError, resolve_ipwo_proxy

    opts["ipwo_zone"] = region
    try:
        url = resolve_ipwo_proxy(opts)
    except IpwoError as e:
        raise DynamicProxyError(str(e)) from e
    return url, f"ipwo/{region}"


def resolve_for_run(
    options: Mapping[str, Any],
    *,
    worker_id: int = 0,
    run_slot: int = 0,
) -> tuple[str, str, CellKey | None]:
    """返回 (proxy_url, label, cell)。多格 fallback + 在途占用至 release_proxy_run。"""
    if not dynamic_proxy_enabled(options):
        return "", "", None

    try:
        max_attempts = max(1, int(options.get("proxy_balance_fallback_attempts") or 6))
    except (TypeError, ValueError):
        max_attempts = 6

    balancer = get_balancer()
    candidates = balancer.rank_candidates(
        options, worker_id=worker_id, run_slot=run_slot, limit=max_attempts
    )
    if not candidates:
        raise DynamicProxyError("无可用代理格（检查 zooproxy/ipwo 是否启用）")

    last_err = ""
    for cell in candidates:
        balancer.acquire_run(cell)
        try:
            url, label = _resolve_cell(options, cell)
            logger.info(
                "动态代理 %s slot=%d worker=%d (balance)",
                label,
                run_slot,
                worker_id,
            )
            return url, label, cell
        except DynamicProxyError as e:
            last_err = str(e)
            balancer.record_outcome(cell, ok=False, err=last_err)
            balancer.release_run(cell)
            logger.warning(
                "代理格 %s/%s 失败，尝试下一格: %s",
                cell[0],
                cell[1],
                last_err[:120],
            )
        except Exception as e:
            last_err = str(e)
            balancer.record_outcome(cell, ok=False, err=last_err)
            balancer.release_run(cell)
            raise

    raise DynamicProxyError(
        f"连续 {len(candidates)} 个代理格均失败，最后: {last_err[:200]}"
    )


def release_proxy_run(cell: CellKey | None) -> None:
    if cell:
        get_balancer().release_run(cell)


def rotate_cell_proxy(options: Mapping[str, Any], cell: CellKey | None) -> str | None:
    """在同一格（厂商×国家）内换一条新出口 IP，用于 warmup 重试。

    不占用也不释放格子（调用方已经持有该格），失败返回 None 让调用方沿用旧代理。
    之所以只在同格内换 sid：换格会打乱在途计数与并发预算，而 warmup 失败的主因
    是「这一个 sid 的住宅 IP 是坏的」，同格换 sid 正好治它。
    """
    if cell is None:
        return None
    try:
        url, _label = _resolve_cell(options, cell)
        return url
    except Exception as e:  # noqa: BLE001 - 换 IP 失败不该中断注册流程
        logger.warning("换出口 IP 失败（沿用旧代理）: %s", str(e)[:120])
        return None


def finish_proxy_run(
    cell: CellKey | None,
    *,
    proxy_label: str,
    ok: bool,
    err: str = "",
) -> None:
    if cell:
        get_balancer().record_outcome(cell, ok=ok, err=err)
        get_balancer().release_run(cell)
    elif proxy_label:
        report_proxy_run_outcome(proxy_label, ok=ok, err=err)


__all__ = [
    "DynamicProxyError",
    "dynamic_proxy_enabled",
    "resolve_for_run",
    "rotate_cell_proxy",
    "release_proxy_run",
    "finish_proxy_run",
    "reset_proxy_balance_session",
    "run_slot_index",
    "parse_proxy_label",
    "report_proxy_run_outcome",
]
