"""'Instance' for app mode: watches the VPN app's adapter instead of running openvpn.exe."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from .i18n import t
from .logging_setup import get_logger
from . import winutil
from .winutil import match_adapter


class AppVpnInstance:
    def __init__(self, proxy_id: str, patterns: Callable[[], list[str]],
                 detect: Callable[[], list[dict]] | None = None, interval: float = 3.0):
        self.id = proxy_id
        self.patterns = patterns
        self.detect = detect or (lambda: winutil.list_adapters())
        self.interval = interval
        self.log = get_logger(proxy_id)
        self.status = "stopped"
        self.state_name = ""
        self.local_ip: str | None = None
        self.remote_ip: str | None = None
        self.adapter: dict | None = None
        self.started_at: float | None = None
        self.connected_at: float | None = None
        self.last_error: str | None = None
        self.lines: deque[str] = deque(maxlen=500)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def pid(self) -> int | None:
        return None

    def poll(self) -> None:
        """Re-detect the adapter and update status (also called by the background thread)."""
        found = match_adapter(self.detect(), self.patterns())
        ip = found["ip"] if found else None
        if ip and not ip.startswith("169.254."):
            if self.local_ip != ip:
                self.connected_at = time.time()
                self.adapter = found
                msg = t("app_vpn_found", name=found["name"], desc=found["description"], ip=ip)
                self.lines.append(time.strftime("%H:%M:%S ") + msg)
                self.log.info(msg)
            self.status, self.state_name, self.local_ip, self.last_error = "connected", "CONNECTED", ip, None
        else:
            if self.status == "connected":
                msg = t("app_vpn_lost")
                self.lines.append(time.strftime("%H:%M:%S ") + msg)
                self.log.warning(msg)
            self.status, self.state_name, self.local_ip, self.connected_at = "connecting", "WAITING_APP", None, None
            self.last_error = t("app_vpn_missing")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001
                self.log.debug("adapter poll failed: %s", exc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.started_at = time.time()
        self.poll()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"appvpn-{self.id}")
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread = None
        self.status, self.state_name, self.local_ip, self.connected_at = "stopped", "STOPPED", None, None

    def restart(self) -> None:
        self.stop()
        self.start()
