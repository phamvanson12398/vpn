"""High-level operations shared by the web UI, the CLI and the monitor."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig, load_config, load_credentials
from .errors import HmaProxyError, NotFoundError
from .health import CheckResult, check_many, get_real_ip
from .i18n import t
from .logging_setup import get_logger, register_secret
from .state import StateStore
from .vpn_ctl import InstanceInfo, VpnController, uptime_seconds


@dataclass
class ActionResult:
    id: str
    ok: bool
    message: str


@dataclass
class CreateReport:
    results: list[ActionResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)


class Manager:
    def __init__(self, cfg: AppConfig, ctl: VpnController | None = None, store: StateStore | None = None):
        self.cfg = cfg
        self.store = store or StateStore(cfg.state_dir)
        self.ctl = ctl or VpnController(cfg, self.store, credentials=lambda: load_credentials(self.cfg))
        if cfg.proxy_auth.enabled:
            register_secret(cfg.proxy_auth.password)

    @classmethod
    def from_path(cls, path: str, **kwargs) -> "Manager":
        store_cfg = load_config(path)  # first pass to know state_dir
        plan = StateStore(store_cfg.state_dir).load().get("plan")
        cfg = load_config(path, saved_plan=plan) if (plan and store_cfg.selection.mode == "random") else store_cfg
        return cls(cfg, **kwargs)

    # ------------------------------------------------------------------ helpers
    def resolve_ids(self, target: str | list[str] | None) -> list[str]:
        """'all'/None -> every known proxy; otherwise validate the given ids."""
        known = set(self.ctl.list_managed()) | {p.id for p in self.cfg.proxies}
        if target in (None, "all", ["all"]):
            return sorted(known)
        ids = [target] if isinstance(target, str) else list(target)
        for pid in ids:
            if pid not in known:
                raise NotFoundError(t("proxy_not_found", id=pid))
        return ids

    def _each(self, action: str, ids: list[str], fn) -> list[ActionResult]:
        out = []
        for pid in ids:
            try:
                fn(pid)
                out.append(ActionResult(pid, True, t("action_done", action=action, id=pid)))
                get_logger(pid).info(t("action_done", action=action, id=pid))
            except HmaProxyError as exc:
                out.append(ActionResult(pid, False, t("action_failed", action=action, id=pid, error=exc)))
                get_logger(pid).error(t("action_failed", action=action, id=pid, error=exc))
        return out

    # ------------------------------------------------------------------ create
    def create(self, ids: list[str] | None = None, reshuffle: bool = False) -> CreateReport:
        cfg = self.cfg
        if cfg.selection.mode == "random" and reshuffle:
            from .config import pick_random_servers

            plan = pick_random_servers(cfg)
            cfg = load_config(cfg.path, saved_plan=plan)
            self.cfg = cfg
            self.ctl.cfg = cfg
        creds = load_credentials(cfg)
        register_secret(creds.password)
        self.ctl.preflight()

        existing = self.ctl.list_managed()
        wanted = [p for p in cfg.proxies if ids is None or p.id in ids]
        if not wanted:
            return CreateReport()
        to_create = [p for p in wanted if p.id not in existing]
        if len(existing) + len(to_create) > cfg.max_connections:
            raise HmaProxyError(t("over_limit_running", existing=len(existing), new=len(to_create),
                                  max=cfg.max_connections), exit_code=2)

        report = CreateReport()
        for p in wanted:
            if p.id in existing:
                report.results.append(ActionResult(p.id, True, t("container_exists", id=p.id, name=p.id)))
        with self.store.transaction() as data:
            if cfg.selection.mode == "random":
                data["plan"] = [{"id": p.id, "ovpn": p.ovpn, "country": p.country} for p in cfg.proxies]
        for p in to_create:
            server = cfg.server_by_file(p.ovpn)
            assert server is not None  # validated by load_config
            try:
                self.ctl.create(p, server, creds, start=True)
            except HmaProxyError as exc:
                report.results.append(ActionResult(p.id, False, str(exc)))
                get_logger(p.id).error(str(exc))
                continue
            now = time.time()
            with self.store.transaction() as data:
                self.store.proxy(data, p.id).update(
                    desired="running", health="starting", started_at=now, ovpn=p.ovpn, tried_servers=[p.ovpn],
                    ip=None, consecutive_failures=0, restart_attempts=0, server_switches=0, next_attempt_at=0.0,
                    last_error=None)
            report.results.append(ActionResult(p.id, True, t("created", id=p.id, country=p.country, ovpn=p.ovpn,
                                                             bind=cfg.bind_address, port=p.http_port)))
        return report

    def wait_ready(self, ids: list[str], timeout: float = 120, poll: float = 5) -> dict[str, CheckResult]:
        """Poll until each proxy answers through the VPN or the timeout expires."""
        deadline = time.time() + timeout
        real_ip = self.real_ip(refresh=True)
        pending = set(ids)
        last: dict[str, CheckResult] = {}
        infos = self.ctl.list_managed()
        while pending and time.time() < deadline:
            targets = [(pid, infos[pid].http_port, infos[pid].socks_port) for pid in sorted(pending)
                       if pid in infos and infos[pid].http_port]
            for r in check_many(self.cfg, targets, real_ip):
                last[r.proxy_id] = r
                if r.ok:
                    pending.discard(r.proxy_id)
                    self._record(r)
            if pending:
                time.sleep(poll)
        return last

    # ------------------------------------------------------------------ lifecycle
    def start(self, target=None) -> list[ActionResult]:
        ids = self.resolve_ids(target)

        def do(pid):
            self.ctl.start(pid)
            self.store.update_proxy(pid, desired="running", health="starting", started_at=time.time(),
                                    consecutive_failures=0, next_attempt_at=0.0)

        return self._each("start", ids, do)

    def stop(self, target=None) -> list[ActionResult]:
        ids = self.resolve_ids(target)

        def do(pid):
            # Record intent first so the monitor does not "recover" it meanwhile.
            self.store.update_proxy(pid, desired="stopped", health="stopped")
            self.ctl.stop(pid)

        return self._each("stop", ids, do)

    def restart(self, target=None) -> list[ActionResult]:
        ids = self.resolve_ids(target)

        def do(pid):
            self.ctl.restart(pid)
            self.store.update_proxy(pid, desired="running", health="starting", started_at=time.time(),
                                    consecutive_failures=0, next_attempt_at=0.0)

        return self._each("restart", ids, do)

    def remove(self, target=None) -> list[ActionResult]:
        ids = [pid for pid in self.resolve_ids(target) if pid in self.ctl.list_managed()]
        if target not in (None, "all") and not ids:
            raise NotFoundError(t("proxy_not_found", id=target))

        def do(pid):
            self.ctl.remove(pid)
            self.store.drop_proxy(pid)
            get_logger(pid).info(t("removed", id=pid))

        return self._each("remove", ids, do)

    # ------------------------------------------------------------------ status
    def real_ip(self, refresh: bool = False) -> str | None:
        if self.cfg.app_mode:
            # The VPN app routes the whole PC, so a direct request already shows the VPN IP:
            # comparing would raise false "leak" alarms. Binding to the VPN adapter is the guarantee here.
            return None
        data = self.store.load()
        cached = data.get("real_ip") or {}
        if not refresh and cached.get("ip") and time.time() - cached.get("at", 0) < 600:
            return cached["ip"]
        try:
            ip = get_real_ip(self.cfg)
        except ConnectionError as exc:
            get_logger().warning(t("real_ip_unknown", error=exc))
            return cached.get("ip")
        with self.store.transaction() as d:
            d["real_ip"] = {"ip": ip, "at": time.time()}
        return ip

    def rows(self, with_containers: bool = True) -> list[dict[str, Any]]:
        """One row per proxy for the UI table / `list`."""
        infos = self.ctl.list_managed() if with_containers else {}
        data = self.store.load()
        ids = sorted(set(infos) | {p.id for p in self.cfg.proxies})
        out = []
        for pid in ids:
            info: InstanceInfo | None = infos.get(pid)
            spec = self.cfg.proxy(pid)
            st = data["proxies"].get(pid, {})
            status = info.status if info else "not created"
            health = st.get("health", "unknown") if info else "-"
            if info and not info.running and st.get("desired") == "stopped":
                health = "stopped"
            out.append({
                "id": pid,
                "country": (info.country if info else None) or (spec.country if spec else ""),
                "ovpn": (info.ovpn if info else None) or (spec.ovpn if spec else ""),
                "http_port": (info.http_port if info else None) or (spec.http_port if spec else None),
                "socks_port": (info.socks_port if info else None) or (spec.socks_port if spec else None),
                "status": status,
                "vpn_state": info.state_name if info else "",
                "tunnel_ip": info.local_ip if info else None,
                "vpn_server": info.remote_ip if info else None,
                "health": health,
                "desired": st.get("desired", "running"),
                "ip": st.get("ip"),
                "socks_ok": st.get("socks_ok"),
                "last_check": st.get("last_check"),
                "last_error": (info.last_error if info and info.last_error else None) or st.get("last_error"),
                "uptime": uptime_seconds(info.connected_at) if info else None,
                "in_config": spec is not None,
                "created": info is not None,
            })
        return out

    def check(self, target=None) -> tuple[str | None, list[CheckResult]]:
        ids = self.resolve_ids(target)
        infos = self.ctl.list_managed()
        real_ip = self.real_ip(refresh=True)
        targets = []
        results: list[CheckResult] = []
        for pid in ids:
            info = infos.get(pid)
            if info is None or info.status != "connected" or not info.http_port:
                results.append(CheckResult(pid, False, error=f"VPN {info.status if info else 'missing'}"))
                continue
            targets.append((pid, info.http_port, info.socks_port))
        results += check_many(self.cfg, targets, real_ip)
        for r in results:
            self._record(r)
        results.sort(key=lambda r: r.proxy_id)
        return real_ip, results

    def _record(self, r: CheckResult) -> None:
        now = time.time()
        with self.store.transaction() as data:
            st = self.store.proxy(data, r.proxy_id)
            st["last_check"] = now
            if r.ok:
                st.update(ip=r.ip, last_ok=now, socks_ok=r.socks_ok, last_error=r.socks_error)
                if st.get("health") not in ("failed",):
                    st["health"] = "healthy"
                st["consecutive_failures"] = 0
            else:
                st["last_error"] = r.error
                st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1

    def logs(self, proxy_id: str, tail: int = 200, socks: bool = False) -> str:
        if proxy_id not in self.ctl.list_managed():
            raise NotFoundError(t("proxy_not_found", id=proxy_id))
        from .logging_setup import redact

        return redact("\n".join(self.ctl.logs(proxy_id, tail=tail)))

    def healthy_backends(self, kind: str = "http") -> list[tuple[str, int]]:
        host = self.cfg.connect_host
        out = []
        for r in self.rows():
            if r["status"] != "connected" or r["health"] != "healthy":
                continue
            port = r["http_port"] if kind == "http" else r["socks_port"]
            if port:
                out.append((host, int(port)))
        return out


def system_ok(mgr: Manager) -> tuple[bool, str, dict]:
    """Can tunnels be started on this PC? (OpenVPN installed, running as admin)."""
    info = mgr.ctl.system_status()
    try:
        mgr.ctl.preflight()
        return True, "", info
    except HmaProxyError as exc:
        return False, str(exc), info
