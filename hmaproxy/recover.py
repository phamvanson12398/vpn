"""Self-healing with exponential backoff (FR-5).

Escalation ladder for one proxy:

1. restart the VPN connection (up to ``recovery.max_restarts`` times), waiting
   ``backoff_base * 2^(n-1)`` seconds (capped at ``backoff_max``) between tries
   so HMA does not see a burst of reconnects;
2. if it is still broken, recreate it with another .ovpn of the same country
   (up to ``recovery.max_server_switches`` different servers);
3. otherwise mark it FAILED, log an error, and keep retrying slowly
   (once every ``backoff_max`` seconds) so a temporary HMA outage heals itself.
"""

from __future__ import annotations

import time
from typing import Callable

from .config import AppConfig, ProxySpec
from .vpn_ctl import InstanceInfo, VpnController
from .errors import HmaProxyError
from .i18n import t
from .logging_setup import get_logger
from .providers import Credentials
from .state import StateStore


def backoff_delay(cfg: AppConfig, attempt: int) -> int:
    """Seconds to wait after attempt number ``attempt`` (1-based) before the next one."""
    r = cfg.recovery
    if attempt <= 0:
        return 0
    return int(min(r.backoff_max_seconds, r.backoff_base_seconds * (2 ** (attempt - 1))))


class Recovery:
    def __init__(self, cfg: AppConfig, ctl: VpnController, store: StateStore,
                 credentials: Callable[[], Credentials], clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.ctl = ctl
        self.store = store
        self.credentials = credentials
        self.clock = clock

    # ------------------------------------------------------------------
    def spec_for(self, info: InstanceInfo) -> ProxySpec:
        spec = self.ctl.specs.get(info.id) or self.cfg.proxy(info.id)
        if spec is not None:
            return spec
        # Proxy created from an older config: rebuild the spec from the saved state.
        return ProxySpec(id=info.id, ovpn=info.ovpn, country=info.country,
                         http_port=info.http_port or 0, socks_port=info.socks_port)

    def recover(self, info: InstanceInfo, reason: str) -> str:
        """Run one recovery step if the backoff allows it. Returns the action taken."""
        log = get_logger(info.id)
        now = self.clock()
        st = self.store.get_proxy(info.id)
        if now < float(st.get("next_attempt_at") or 0):
            wait = int(float(st["next_attempt_at"]) - now)
            log.debug(t("recover_backoff", seconds=wait))
            return "backoff"

        r = self.cfg.recovery
        attempts = int(st.get("restart_attempts") or 0)
        switches = int(st.get("server_switches") or 0)
        current = info.ovpn or st.get("ovpn") or ""
        tried = list(dict.fromkeys([*(st.get("tried_servers") or []), current]))
        common = {"consecutive_failures": 0, "started_at": now, "last_error": reason}

        if st.get("health") != "failed" and attempts < r.max_restarts:
            attempts += 1
            log.warning(t("recover_restart", attempt=attempts, max=r.max_restarts) + f" ({reason})")
            action = "restart"
            ok = self._restart(info, log)
            self.store.update_proxy(info.id, health="starting", restart_attempts=attempts,
                                    next_attempt_at=now + backoff_delay(self.cfg, attempts), **common)
            return action if ok else "restart-error"

        if st.get("health") != "failed" and r.switch_server and switches < r.max_server_switches:
            server = self._pick_alternative(current, tried)
            if server is not None:
                switches += 1
                log.warning(t("recover_switch", old=current or "?", new=server.filename))
                spec = self.spec_for(info)
                try:
                    self.ctl.switch_server(spec, server, self.credentials())
                    ok = True
                except HmaProxyError as exc:
                    log.error(str(exc))
                    ok = False
                tried.append(server.filename)
                self.store.update_proxy(
                    info.id, health="starting", restart_attempts=0, server_switches=switches,
                    tried_servers=tried, ovpn=server.filename,
                    next_attempt_at=now + backoff_delay(self.cfg, switches + r.max_restarts), **common,
                )
                return "switch" if ok else "switch-error"

        # Give up (for now): mark failed, keep retrying slowly.
        if st.get("health") != "failed":
            log.error(t("recover_failed", reason=reason))
        self._restart(info, log)
        self.store.update_proxy(info.id, health="failed", next_attempt_at=now + r.backoff_max_seconds,
                                consecutive_failures=0, started_at=now, last_error=reason)
        return "failed"

    def _restart(self, info: InstanceInfo, log) -> bool:
        try:
            if info.running:
                self.ctl.restart(info.id)
            else:
                self.ctl.start(info.id)
            return True
        except HmaProxyError as exc:
            log.error(str(exc))
            return False

    def _pick_alternative(self, current_file: str, tried: list[str]):
        current = self.cfg.server_by_file(current_file)
        if current is None:
            return None
        alts = self.cfg.provider.alternatives(self.cfg.servers, current, exclude=set(tried))
        return alts[0] if alts else None

    # ------------------------------------------------------------------
    def mark_healthy(self, proxy_id: str, ip: str, socks_ok: bool | None) -> None:
        st = self.store.get_proxy(proxy_id)
        if st.get("health") in ("starting", "unhealthy", "failed") and st.get("restart_attempts", 0) + st.get("server_switches", 0):
            get_logger(proxy_id).info(t("recovered", ip=ip))
        now = self.clock()
        self.store.update_proxy(
            proxy_id, health="healthy", ip=ip, socks_ok=socks_ok, last_check=now, last_ok=now,
            last_error=None, consecutive_failures=0, restart_attempts=0, server_switches=0,
            tried_servers=[], next_attempt_at=0.0,
        )
