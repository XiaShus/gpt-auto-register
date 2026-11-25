"""CF 临时邮箱：按域限制并发 wait_for_otp，避免 Worker 被轮询打满。"""
from __future__ import annotations

import threading
import time
from typing import Optional

_lock = threading.Lock()
_sems: dict[str, threading.Semaphore] = {}
_per_domain = 4
_acquire_timeout = 600.0


def configure(*, per_domain: int = 4, acquire_timeout: float = 600.0) -> None:
    global _per_domain, _acquire_timeout
    _per_domain = max(1, int(per_domain))
    _acquire_timeout = max(30.0, float(acquire_timeout))


def _sem(domain: str) -> threading.Semaphore:
    d = (domain or "?").strip().lower()
    with _lock:
        if d not in _sems:
            _sems[d] = threading.Semaphore(_per_domain)
        return _sems[d]


def acquire_otp_slot(domain: str, timeout: Optional[float] = None) -> bool:
    deadline = time.time() + (timeout if timeout is not None else _acquire_timeout)
    sem = _sem(domain)
    while time.time() < deadline:
        if sem.acquire(blocking=False):
            return True
        time.sleep(0.25)
    return False


def release_otp_slot(domain: str) -> None:
    try:
        _sem(domain).release()
    except ValueError:
        pass
