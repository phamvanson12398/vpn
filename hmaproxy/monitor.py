"""Periodic health monitoring + self-healing loop (FR-4, FR-5)."""

from __future__ import annotations

import threading
import time

from .config import load_credentials
from .errors import HmaProxyError
from .health import check_many
from .i18n import t
from .logging_setup import get_logger
from .recover import Recovery
from .service import Manager


class Monitor:
    def __init__(self, mgr: Manager, clock=time.time):
        self.mgr = mgr
        self.clock = clock
        self.recovery = Recovery(mgr.cfg, mgr.ctl, mgr.store, lambda: load_credentials(mgr.cfg), clock=clock)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.ticks = 0
        self.last_tick: float | None = None
        self.last_error: str | None = None
        self.log = get_logger("monitor")

    # ------------------------------------------------------------------
    def tick(self) -> None:
        mgr, cfg, store = self.mgr, self.mgr.cfg, self.mgr.store
        now = self.clock()
        infos = mgr.ctl.list_managed()
        real_ip = mgr.real_ip(refresh=(self.ticks % 10 == 0))
        self.ticks += 1
        data = store.load()

        targets = []
        for pid, info in sorted(infos.items()):
            st = store.proxy(data, pid)
            if st.get("desired") == "stopped":
                continue
            if not info.running:
                reason = t("container_down", status=info.status)
                get_logger(pid).warning(reason)
                self.recovery.recover(info, reason)
                continue
            if info.socks_status not in (None, "running"):
                get_logger(pid).warning(t("socks_restart"))
                try:
                    mgr.ctl.restart_socks(pid)
                except HmaProxyError as exc:
                    get_logger(pid).error(str(exc))
            if info.http_port:
                targets.append((pid, info.http_port, info.socks_port))

        results = check_many(cfg, targets, real_ip)
        grace = cfg.health.startup_grace_seconds
        threshold = cfg.health.failure_threshold
        for r in results:
            log = get_logger(r.proxy_id)
            info = infos[r.proxy_id]
            st = store.get_proxy(r.proxy_id)
            started = st.get("started_at")
            in_grace = grace > 0 and bool(started) and now - float(started) < grace

            if r.leak:
                # Should never happen with gluetun's kill switch - stop it and require a human.
                log.error(r.error or r.socks_error or "IP leak")
                store.update_proxy(r.proxy_id, desired="stopped", health="failed", last_error=r.error or r.socks_error,
                                   last_check=now)
                try:
                    mgr.ctl.stop(r.proxy_id)
                except HmaProxyError as exc:
                    log.error(str(exc))
                continue

            if r.ok:
                if st.get("ip") and st.get("ip") != r.ip:
                    log.info("exit IP changed %s -> %s", st.get("ip"), r.ip)
                self.recovery.mark_healthy(r.proxy_id, r.ip or "", r.socks_ok)
                for other in r.duplicate_of:
                    log.warning(t("ip_duplicate", id=r.proxy_id, ip=r.ip, other=other))
                if r.socks_ok is False:
                    log.warning(t("socks_restart") + f": {r.socks_error}")
                    store.update_proxy(r.proxy_id, last_error=t("socks_fail", error=r.socks_error))
                    try:
                        mgr.ctl.restart_socks(r.proxy_id)
                    except HmaProxyError as exc:
                        log.error(str(exc))
                continue

            failures = int(st.get("consecutive_failures") or 0) + 1
            health = st.get("health")
            if health != "failed":
                health = "starting" if in_grace else ("unhealthy" if failures >= threshold else health or "unknown")
            store.update_proxy(r.proxy_id, consecutive_failures=failures, last_error=r.error, last_check=now,
                               health=health)
            log.info(t("check_fail", id=r.proxy_id, error=r.error))
            if not in_grace and failures >= threshold:
                reason = t("unhealthy", count=failures)
                log.warning(reason)
                self.recovery.recover(info, f"{reason}: {r.error}")

    # ------------------------------------------------------------------
    def run(self) -> None:
        interval = self.mgr.cfg.health.interval_seconds
        self.log.info(t("monitor_start", count=len(self.mgr.cfg.proxies), interval=interval))
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except HmaProxyError as exc:
                self.last_error = str(exc)
                self.log.error(str(exc))
            except Exception as exc:  # keep the daemon alive no matter what
                self.last_error = str(exc)
                self.log.exception("monitor tick failed: %s", exc)
            self.last_tick = self.clock()
            self._wake.wait(interval)
            self._wake.clear()
        self.log.info(t("monitor_stop"))

    def run_in_thread(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="hmaproxy-monitor", daemon=True)
        th.start()
        return th

    def poke(self) -> None:
        """Run the next tick now instead of waiting for the interval."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
