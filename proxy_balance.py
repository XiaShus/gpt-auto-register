"""动态代理 (厂商 × 国家) 负载均衡：先验矩阵权重 + 在线 EWMA + 在途上限 + 失败冷却。

目标：c=12 并发下避免挤在同一 (zooproxy|ipwo, FR) 格子，预检/warmup 失败快速换格。
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)

CellKey = tuple[str, str]  # (provider, country)

EXCLUDED_COUNTRIES = frozenset({"UK"})

# register-matrix 20261012 成功率 → 先验权重 (0~1)
_PRIOR: dict[CellKey, float] = {
    ("zooproxy", "FR"): 1.00,
    ("zooproxy", "JP"): 0.70,
    ("zooproxy", "BR"): 0.60,
    ("zooproxy", "CA"): 0.60,
    ("zooproxy", "DE"): 0.50,
    ("zooproxy", "NA"): 0.50,
    ("zooproxy", "US"): 0.40,
    ("ipwo", "BR"): 0.70,
    ("ipwo", "DE"): 0.50,
    ("ipwo", "NA"): 0.50,
    ("ipwo", "FR"): 0.40,
    ("ipwo", "JP"): 0.40,
    ("ipwo", "US"): 0.40,
    ("ipwo", "CA"): 0.30,
}

DEFAULT_ZOO_COUNTRIES = ("FR", "JP", "BR", "CA", "DE", "NA")
DEFAULT_IPWO_COUNTRIES = ("BR", "DE", "NA", "FR", "JP")

# 每格同时进行的 run 上限（整 run 生命周期占用，非仅建连）
_DEFAULT_MAX_IN_FLIGHT = 2
_EWMA_ALPHA = 0.08
_PENALTY_SEC_PRECHECK = 25.0
_PENALTY_SEC_WARMUP = 40.0
_PENALTY_SEC_407 = 120.0
_PENALTY_SEC_409 = 0.0  # 409 多为无 oai-did 会话；warmup 已扣分，勿长冷却整格


def _prior(provider: str, country: str) -> float:
    return _PRIOR.get((provider, country.upper()), 0.35)


# 邮件层错误：收信/OTP 超时与代理质量无关，不能拿它扣代理格的分，
# 也不能让它进冷却 —— 否则邮件侧一抖动，代理矩阵会被自己的统计饿死。
_MAIL_LAYER_HINTS = (
    "otp timeout", "otp slot wait", "imap otp", "outlook otp", "outlook imap",
    "outlook graph", "graph 失败", "无可用 imap 凭据", "basic authentication is disabled",
)


def is_mail_layer_error(err: str) -> bool:
    s = (err or "").lower()
    return any(h in s for h in _MAIL_LAYER_HINTS)


class ProxyLoadBalancer:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._in_flight: dict[CellKey, int] = {}
        self._ewma_ok: dict[CellKey, float] = {}
        self._penalty_until: dict[CellKey, float] = {}
        self._rng = random.Random()
        self._seq = 0

    def reset_session(self) -> None:
        with self._lock:
            self._in_flight.clear()
            self._penalty_until.clear()
            self._seq = 0

    @staticmethod
    def _parse_countries(text: str, default: tuple[str, ...]) -> list[str]:
        raw = (text or "").strip()
        if not raw:
            return [c for c in default if c not in EXCLUDED_COUNTRIES]
        out: list[str] = []
        for part in re.split(r"[,;\s]+", raw):
            cc = part.strip().upper()
            if cc and cc not in EXCLUDED_COUNTRIES and cc not in out:
                out.append(cc)
        return out if out else [c for c in default if c not in EXCLUDED_COUNTRIES]

    def _enabled_cells(self, options: Mapping[str, Any]) -> list[CellKey]:
        zoo = bool(options.get("zooproxy_enabled"))
        ipwo = bool(options.get("ipwo_enabled"))
        multi = bool(options.get("proxy_multi_country", True))
        cells: list[CellKey] = []

        if zoo:
            if multi:
                for cc in self._parse_countries(
                    str(options.get("proxy_countries_zoo") or ""),
                    DEFAULT_ZOO_COUNTRIES,
                ):
                    cells.append(("zooproxy", cc))
            else:
                cc = (options.get("zooproxy_gateway_region") or "US").strip().upper()
                if cc not in EXCLUDED_COUNTRIES:
                    cells.append(("zooproxy", cc))
        if ipwo:
            if multi:
                for cc in self._parse_countries(
                    str(options.get("proxy_countries_ipwo") or ""),
                    DEFAULT_IPWO_COUNTRIES,
                ):
                    cells.append(("ipwo", cc))
            else:
                cc = (options.get("ipwo_zone") or "NA").strip().upper()
                if cc not in EXCLUDED_COUNTRIES:
                    cells.append(("ipwo", cc))
        return cells

    def _max_in_flight(self, options: Mapping[str, Any], n_cells: int) -> int:
        try:
            c = max(1, int(options.get("concurrency") or 12))
        except (TypeError, ValueError):
            c = 12
        try:
            cap = int(options.get("proxy_max_in_flight_per_cell") or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            return cap
        if n_cells <= 0:
            return _DEFAULT_MAX_IN_FLIGHT
        # 12 worker / ~11 格 → 至少 1，最多 2，避免单格挤爆
        return max(1, min(2, (c + n_cells - 1) // n_cells))

    def _ewma(self, cell: CellKey) -> float:
        if cell not in self._ewma_ok:
            return _prior(cell[0], cell[1])
        return self._ewma_ok[cell]

    def _score(
        self,
        cell: CellKey,
        *,
        max_in_flight: int,
        run_slot: int,
        jitter: float,
    ) -> float:
        provider, country = cell
        now = time.time()
        if self._penalty_until.get(cell, 0) > now:
            return 0.0
        inflight = self._in_flight.get(cell, 0)
        if inflight >= max_in_flight:
            return 0.0
        prior = _prior(provider, country)
        ewma = self._ewma(cell)
        load = 1.0 / (1.0 + inflight)
        # 轻微打散：同分格子用 run_slot 旋转，再加极小 jitter 破 ties
        tie = 1.0 + 0.02 * ((run_slot + hash(cell)) % 7)
        return prior * ewma * load * tie * (1.0 + jitter)

    def rank_candidates(
        self,
        options: Mapping[str, Any],
        *,
        worker_id: int,
        run_slot: int,
        limit: int = 6,
    ) -> list[CellKey]:
        cells = self._enabled_cells(options)
        if not cells:
            return []
        max_if = self._max_in_flight(options, len(cells))
        with self._lock:
            self._seq += 1
            jitter = self._rng.uniform(-0.03, 0.03)
            scored: list[tuple[float, CellKey]] = []
            for cell in cells:
                s = self._score(cell, max_in_flight=max_if, run_slot=run_slot + worker_id, jitter=jitter)
                if s > 0:
                    scored.append((s, cell))
            scored.sort(key=lambda x: (-x[0], x[1]))
            out = [c for _, c in scored[: max(1, limit)]]
            if not out:
                # 全在冷却或满负载：选惩罚最早结束、在途最少的格
                fallback = sorted(
                    cells,
                    key=lambda c: (
                        self._penalty_until.get(c, 0),
                        self._in_flight.get(c, 0),
                        -_prior(c[0], c[1]),
                    ),
                )
                out = fallback[: max(1, min(limit, len(fallback)))]
            return out

    def acquire_run(self, cell: CellKey) -> None:
        with self._lock:
            self._in_flight[cell] = self._in_flight.get(cell, 0) + 1

    def release_run(self, cell: CellKey) -> None:
        with self._lock:
            n = self._in_flight.get(cell, 0) - 1
            if n <= 0:
                self._in_flight.pop(cell, None)
            else:
                self._in_flight[cell] = n

    def record_outcome(self, cell: CellKey, *, ok: bool, err: str = "") -> None:
        s = (err or "").lower()
        mail_layer = is_mail_layer_error(s)
        with self._lock:
            # 邮件层失败不反映代理质量：即不动 EWMA，也不进冷却。
            if not mail_layer:
                prev = self._ewma(cell)
                target = 1.0 if ok else 0.0
                self._ewma_ok[cell] = prev + _EWMA_ALPHA * (target - prev)
            if ok or mail_layer:
                return
            penalty = _PENALTY_SEC_WARMUP
            if "407" in s:
                penalty = _PENALTY_SEC_407
            elif "预检" in err or "precheck" in s or "proxy_build" in s:
                penalty = _PENALTY_SEC_PRECHECK
            elif "409" in s or "invalid_state" in s:
                penalty = _PENALTY_SEC_409
            elif "warmup" in s or "oai-did" in s:
                penalty = _PENALTY_SEC_WARMUP
            if penalty <= 0:
                return
            until = time.time() + penalty
            old = self._penalty_until.get(cell, 0)
            if until > old:
                self._penalty_until[cell] = until
                logger.info(
                    "代理格 %s/%s 冷却 %.0fs（%s）",
                    cell[0],
                    cell[1],
                    penalty,
                    (err or "")[:80],
                )

    def snapshot_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "in_flight": {
                    f"{a}/{b}": n for (a, b), n in self._in_flight.items()
                },
                "ewma_ok": {f"{a}/{b}": round(v, 3) for (a, b), v in self._ewma_ok.items()},
                "penalty_until": {
                    f"{a}/{b}": round(t - time.time(), 1)
                    for (a, b), t in self._penalty_until.items()
                    if t > time.time()
                },
            }


_BALANCER = ProxyLoadBalancer()


def get_balancer() -> ProxyLoadBalancer:
    return _BALANCER


def parse_proxy_label(label: str) -> CellKey | None:
    p = (label or "").strip()
    if "/" not in p:
        return None
    prov, cc = p.split("/", 1)
    prov = prov.strip().lower()
    cc = cc.strip().upper()
    if prov in ("zooproxy", "ipwo") and cc and cc not in EXCLUDED_COUNTRIES:
        return (prov, cc)
    return None


def report_proxy_run_outcome(proxy_label: str, *, ok: bool, err: str = "") -> None:
    cell = parse_proxy_label(proxy_label)
    if cell:
        get_balancer().record_outcome(cell, ok=ok, err=err)
