"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个 worker 按 worker index 取一个代理（round-robin），避免同 IP 多号
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Optional

from . import db, registrar
from mail_providers import MailProviderError, get_provider_class

logger = logging.getLogger("auto_loop")

try:
    from proxy_pool import (
        DynamicProxyError,
        dynamic_proxy_enabled,
        finish_proxy_run,
        reset_proxy_balance_session,
        resolve_for_run,
        rotate_cell_proxy,
        run_slot_index,
    )
except ImportError:
    dynamic_proxy_enabled = None  # type: ignore
    resolve_for_run = None  # type: ignore
    run_slot_index = None  # type: ignore
    finish_proxy_run = None  # type: ignore
    rotate_cell_proxy = None  # type: ignore
    reset_proxy_balance_session = None  # type: ignore

    class DynamicProxyError(RuntimeError):
        pass


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


# 账密被拒 / 欠费不会在 45–90s 内自愈；这类错误自动恢复只会空转熔断。
_FATAL_PROXY_ENV_HINTS = (
    "curl: (97)",
    "user was rejected by the socks5",
    "response 402",
    "connect tunnel failed, response 402",
    "需要提取的ip数量为0",
    "需要提取的ip数量为 0",
)


def _is_fatal_proxy_env(err: str) -> bool:
    s = (err or "").lower()
    return any(h in s for h in _FATAL_PROXY_ENV_HINTS)


def _parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（1-100，受 VPS/上游限流约束）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._consecutive_otp_fails = 0
        self._circuit_break_threshold = 3
        self._otp_circuit_break_threshold = 8
        self._last_break_reason = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._concurrency: int = 1
        # 目标成功数：0 = 不限量（保持旧行为）；>0 时累计成功达标即自动停止
        self._target_count: int = 0
        self._auto_resume_timer: Optional[threading.Timer] = None

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        manage_to_join: Optional[threading.Thread] = None
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                # 点「开始」= 新批次：先停掉熔断/暂停中的旧会话（避免卡 paused 无法启动）
                self._stop_event.set()
                self._pause_event.clear()
                self._cancel_auto_resume_timer()
                manage_to_join = self._manage_thread
        if manage_to_join is not None and manage_to_join.is_alive():
            manage_to_join.join(timeout=90)
        with self._lock:
            if self._state != AutoLoopState.STOPPED:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._last_message = "上一会话已结束，准备新批次"
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._worker_status.clear()
            self._consecutive_network_fails = 0
            self._consecutive_otp_fails = 0
            self._cancel_auto_resume_timer()
            self._last_message = "auto-loop 启动"
            if reset_proxy_balance_session is not None:
                reset_proxy_balance_session()
            try:
                from mail_otp_limit import configure as configure_otp_limit

                configure_otp_limit(
                    per_domain=int(self._options.get("mail_otp_per_domain") or 4),
                )
            except ImportError:
                pass
            # 解析并发参数
            self._concurrency = max(1, min(100, int(self._options.get("concurrency") or 1)))
            self._otp_circuit_break_threshold = max(
                3,
                int(self._options.get("otp_circuit_break_threshold") or 8),
            )
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = _parse_proxy_pool(pool_text)
            # 目标成功数（0=不限量）
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            default_break = 6 if self._dynamic_proxy_enabled() else 3
            self._circuit_break_threshold = max(
                1,
                int(self._options.get("circuit_break_threshold") or default_break),
            )
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            self._cancel_auto_resume_timer()
            self._last_message = "已请求停止（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def _cancel_auto_resume_timer(self) -> None:
        t = self._auto_resume_timer
        self._auto_resume_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "proxy_label": info.get("proxy_label", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            proxy_balance = {}
            try:
                from proxy_balance import get_balancer

                proxy_balance = get_balancer().snapshot_stats()
            except ImportError:
                pass
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
                "proxy_balance": proxy_balance,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _dynamic_proxy_enabled(self) -> bool:
        if dynamic_proxy_enabled is not None:
            return dynamic_proxy_enabled(self._options)
        return bool(
            self._options.get("zooproxy_enabled") or self._options.get("ipwo_enabled")
        )

    def _proxy_for_worker(self, worker_id: int) -> str:
        """静态池：按 worker 固定一条；动态代理在每次开 run 时再拉新 IP + 国家。"""
        if self._dynamic_proxy_enabled():
            return ""
        if self._proxy_pool:
            return self._proxy_pool[worker_id % len(self._proxy_pool)]
        return self._options.get("proxy", "") or ""

    @staticmethod
    def _static_proxy_label(proxy: str) -> str:
        p = (proxy or "").strip()
        if not p:
            return "直连"
        if "@" in p:
            return p.split("@", 1)[-1][:56]
        return p[:56]

    def _proxy_for_run(self, worker_id: int, static_proxy: str) -> tuple[str, str, Any]:
        if not self._dynamic_proxy_enabled():
            p = static_proxy or ""
            return p, self._static_proxy_label(p), None
        if resolve_for_run is None or run_slot_index is None:
            raise DynamicProxyError("proxy_pool 模块未加载")
        with self._lock:
            slot = run_slot_index(
                worker_id=worker_id,
                registered_ok=self._registered_ok,
                registered_fail=self._registered_fail,
            )
        proxy, label, cell = resolve_for_run(
            self._options, worker_id=worker_id, run_slot=slot
        )
        return proxy, label, cell

    def _record_finish(self, ok: bool, category: str, err: str = ""):
        """worker 结束一个 run 后调，更新计数 + 熔断。"""
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            # 目标数量：累计成功达标 → 触发停止（stop_event 幂等，多 worker 同时命中也安全）
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，自动停止"
                    f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            logger.info(f"已达目标 {self._target_count} 个成功，触发自动停止")
            self._broadcast("state", self._snapshot())
            return

        if trigger_break:
            fatal = _is_fatal_proxy_env(err)
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                n = self._consecutive_network_fails
                if fatal:
                    self._last_break_reason = (
                        f"连续 {n} 次代理鉴权/欠费错误，已暂停且不自动恢复"
                        f"（{str(err or '')[:120]}）"
                    )
                else:
                    self._last_break_reason = (
                        f"连续 {n} 次网络/环境错误，"
                        f"自动暂停（号已自动 release，请检查代理后点恢复）"
                    )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
                auto_resume = bool(self._options.get("auto_resume_on_network", True)) and not fatal
                delay = float(self._options.get("auto_resume_delay_seconds") or 45)
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})
            if auto_resume and delay > 0:
                self._schedule_auto_resume(delay)

    def _schedule_auto_resume(self, delay: float) -> None:
        self._cancel_auto_resume_timer()

        def _fire():
            with self._lock:
                if self._stop_event.is_set():
                    return
                if self._state != AutoLoopState.PAUSED:
                    return
                self._pause_event.clear()
                self._state = AutoLoopState.RUNNING
                self._last_message = (
                    f"网络熔断后 {int(delay)}s 自动恢复（可手动停止）"
                )
            logger.info(self._last_message)
            self._broadcast("state", self._snapshot())

        t = threading.Timer(delay, _fire)
        t.daemon = True
        self._auto_resume_timer = t
        t.start()
        self._set_message(
            f"{self._last_break_reason}；约 {int(delay)}s 后自动恢复"
        )

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 高并发下 1s 错开会让 100 worker 冷启动超过 1.5 分钟
                time.sleep(0.12)
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._last_message = (
                    f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        proxy = self._proxy_for_worker(worker_id)
        logger.info(f"[worker-{worker_id}] 启动 (proxy={proxy or '直连'})")

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # 目标数量闸门：已成功 + 在跑的（复用 _worker_status 当在跑数）≥ 目标 → 本 worker 退出
            # 不新增易泄漏的计数器；_worker_status 已在锁内正常维护，最大限度压低超额
            with self._lock:
                if self._target_count and (
                    self._registered_ok + len(self._worker_status) >= self._target_count
                ):
                    logger.info(
                        f"[worker-{worker_id}] 目标 {self._target_count} 已锁定，退出"
                    )
                    return

            # claim 下一个号。要不要走号池由 provider 的 pooled 决定，
            # 非池化的（CF 这类自己造地址的）用虚拟占位。
            mail_source = db.get_setting("mail_source", "outlook")
            try:
                pooled = get_provider_class(mail_source).pooled
            except MailProviderError as e:
                logger.error(f"[worker-{worker_id}] {e}，停止")
                self._set_message(str(e))
                return
            if pooled:
                account = db.claim_next(kind=mail_source)
            else:
                account = {
                    "email": f"{mail_source}_placeholder_"
                             f"{int(time.time())}_{worker_id}@placeholder.local",
                    "password": "", "client_id": "", "refresh_token": "",
                    "relay_url": "", "kind": mail_source,
                    # CF 多域：同一 worker 固定用一个域，并发间分散声誉/限流
                    "cf_domain_index": worker_id,
                }
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，挂起等待新号（不退出）"
                    )
                    logger.info(f"[worker-{worker_id}] 号池空，挂起等待新号")
                # 等 3s 再试；不要因短暂空池/claim 争用把 worker 杀掉
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            # 给这个 run 注入代理（ZooProxy：每个号拉一条新 IP）
            run_options = dict(self._options)
            proxy_cell = None
            try:
                run_proxy, proxy_label, proxy_cell = self._proxy_for_run(worker_id, proxy)
            except DynamicProxyError as e:
                logger.error(f"[worker-{worker_id}] 动态代理提取失败: {e}")
                if pooled:
                    db.release_unused(account["email"])
                self._record_finish(False, "network", err=str(e))
                idle_wait = float(self._options.get("zooproxy_fail_sleep") or 15)
                for _ in range(int(idle_wait * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            if run_proxy:
                run_options["proxy"] = run_proxy
                if proxy_cell is not None:
                    # warmup 拿不到 oai-did 时换一条新出口 IP（同格换 sid），
                    # 而不是拿同一个粘性坏 IP 反复重试。默认参数绑定格子，
                    # 避免闭包捕获到下一轮循环的 proxy_cell。
                    run_options["proxy_rotate"] = (
                        lambda cell=proxy_cell: rotate_cell_proxy(self._options, cell)
                    )

            # 启一个 run
            try:
                run_id = registrar.start_registration(account, run_options)
            except Exception as e:
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if finish_proxy_run is not None and proxy_cell is not None:
                    finish_proxy_run(
                        proxy_cell,
                        proxy_label=proxy_label,
                        ok=False,
                        err=str(e),
                    )
                    proxy_cell = None
                if pooled:
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": run_proxy,
                    "proxy_label": proxy_label,
                    "proxy_cell": proxy_cell,
                    "started_at": time.time(),
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": run_proxy,
                "proxy_label": proxy_label,
            })

            ok, category, err_text = self._wait_run_finish(run_id)

            with self._lock:
                winfo = self._worker_status.pop(worker_id, {})
            if finish_proxy_run is not None:
                finish_proxy_run(
                    winfo.get("proxy_cell"),
                    proxy_label=winfo.get("proxy_label") or proxy_label,
                    ok=ok,
                    err=err_text or "",
                )
            self._record_finish(ok, category, err=err_text or "")
            err_l = (err_text or "").lower()
            if not ok and ("otp timeout" in err_l or "otp slot wait" in err_l):
                self._on_otp_fail(err_text)
            elif ok:
                with self._lock:
                    self._consecutive_otp_fails = 0
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
            })

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _on_otp_fail(self, err_text: str) -> None:
        trigger = False
        with self._lock:
            self._consecutive_otp_fails += 1
            if (
                self._consecutive_otp_fails >= self._otp_circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            ):
                trigger = True
        if not trigger:
            return
        delay = float(self._options.get("otp_circuit_resume_seconds") or 120)
        with self._lock:
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_break_reason = (
                f"连续 {self._consecutive_otp_fails} 次 OTP/收信超时，"
                f"邮件层可能饱和，自动暂停 {int(delay)}s"
            )
            self._last_message = self._last_break_reason
            self._consecutive_otp_fails = 0
        logger.warning("%s (%s)", self._last_break_reason, err_text[:80])
        self._broadcast("circuit_break", {"reason": self._last_break_reason, "kind": "otp"})
        if bool(self._options.get("auto_resume_on_network", True)) and delay > 0:
            self._schedule_auto_resume(delay)

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str, str]:
        """轮询 runs 表，等 run 跑完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False, "", ""
            con = db._conn()
            cur = con.execute(
                "SELECT status, error_category, error FROM runs WHERE run_id=?",
                (run_id,),
            )
            row = cur.fetchone()
            if row:
                st = row["status"]
                if st == "done":
                    return True, "", ""
                if st == "failed":
                    return False, (row["error_category"] or ""), (row["error"] or "")
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        return False, "", "run wait timeout"


# 全局单例
CONTROLLER = AutoLoopController()
