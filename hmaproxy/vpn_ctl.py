"""Controller for all VPN tunnels + their proxy ports (replaces Docker).

A proxy ``<id>`` is: one virtual adapter ``<adapter_prefix>-<id>``, one
openvpn.exe process, and one HTTP (+ optional SOCKS5) listener bound to the
tunnel's IP. Definitions are persisted in state.json so proxies come back
after a restart of the program or of Windows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .config import AppConfig, ProxySpec
from .dns_resolve import TunnelResolver
from .errors import HmaProxyError, NotFoundError
from .i18n import t
from .logging_setup import get_logger
from .openvpn import OpenVPNInstance, OpenVPNSettings
from .providers import Credentials, Server
from .proxy_server import Auth, ProxyHub, TunnelProxy
from .state import StateStore
from .winutil import AdapterManager, KillOnCloseJob, find_openvpn, is_admin, kill_pid, openvpn_version


@dataclass
class InstanceInfo:
    id: str
    status: str                     # connected | connecting | stopped | exited | missing
    ovpn: str = ""
    country: str = ""
    http_port: int | None = None
    socks_port: int | None = None
    local_ip: str | None = None
    remote_ip: str | None = None
    state_name: str = ""
    started_at: float | None = None
    connected_at: float | None = None
    last_error: str | None = None
    pid: int | None = None
    socks_status: str | None = None
    labels: dict = field(default_factory=dict)

    @property
    def running(self) -> bool:
        """Process alive (connected or still connecting)."""
        return self.status in ("connected", "connecting")


class Instance(Protocol):  # what the controller needs from OpenVPNInstance (lets tests plug a fake)
    id: str
    status: str
    state_name: str
    local_ip: str | None
    remote_ip: str | None
    started_at: float | None
    connected_at: float | None
    last_error: str | None
    lines: object

    @property
    def pid(self) -> int | None: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 10.0) -> None: ...
    def restart(self) -> None: ...


class VpnController:
    def __init__(self, cfg: AppConfig, store: StateStore, credentials: Callable[[], Credentials],
                 hub: ProxyHub | None = None, instance_factory: Callable[..., Instance] | None = None,
                 adapters: AdapterManager | None = None, check_system: bool = True):
        self.cfg = cfg
        self.store = store
        self.credentials = credentials
        self.hub = hub or ProxyHub()
        self.exe = find_openvpn(cfg.openvpn.path)
        self.adapters = adapters or AdapterManager(self.exe, cfg.openvpn.driver)
        self.instance_factory = instance_factory or self._real_instance
        self.check_system = check_system
        self.job = KillOnCloseJob()
        self.resolver = TunnelResolver(cfg.dns.servers, cfg.dns.timeout_seconds, cfg.dns.cache_seconds)
        self.instances: dict[str, Instance] = {}
        self.specs: dict[str, ProxySpec] = {}
        self.log = get_logger()

    # ------------------------------------------------------------------ system
    def system_status(self) -> dict:
        return {
            "openvpn": str(self.exe) if self.exe else None,
            "openvpn_version": self._version(),
            "admin": is_admin(),
            "driver": self.cfg.openvpn.driver,
        }

    def _version(self) -> str:
        if not hasattr(self, "_version_cache"):
            self._version_cache = openvpn_version(self.exe) if self.exe else ""
        return self._version_cache

    def preflight(self) -> None:
        if not self.check_system or self.cfg.app_mode:
            return  # app mode needs neither OpenVPN nor admin rights
        if self.exe is None:
            raise HmaProxyError(t("openvpn_missing"), exit_code=3)
        if not is_admin():
            raise HmaProxyError(t("need_admin"), exit_code=3)

    def adapter_name(self, proxy_id: str) -> str:
        return f"{self.cfg.openvpn.adapter_prefix}-{proxy_id}"

    def _real_instance(self, spec: ProxySpec, server: Server):
        if self.cfg.app_mode:
            from .app_vpn import AppVpnInstance

            return AppVpnInstance(spec.id, patterns=lambda: self.cfg.adapter_patterns())
        settings = OpenVPNSettings(exe=self.exe, driver=self.cfg.openvpn.driver,
                                   route_metric=self.cfg.openvpn.route_metric,
                                   verbosity=self.cfg.openvpn.verbosity,
                                   extra_lines=list(self.cfg.openvpn.extra_config), work_dir=self.cfg.state_dir)
        return OpenVPNInstance(spec.id, server.path, self.adapter_name(spec.id), settings, self.credentials,
                               job=self.job)

    # ------------------------------------------------------------------ persistence
    def restore(self) -> list[str]:
        """Re-create proxies saved in state.json; start those that should run. Returns started ids."""
        data = self.store.load()
        started = []
        for pid, st in sorted(data["proxies"].items()):
            saved = st.get("spec")
            if not saved:
                continue
            kill_pid(int(st.get("pid") or 0))  # orphan from a crash, if any
            server = self.cfg.server_by_file(saved["ovpn"])
            if server is None:
                get_logger(pid).error(t("ovpn_missing", id=pid, ovpn=saved["ovpn"], dir=self.cfg.ovpn_dir))
                continue
            spec = ProxySpec(id=pid, ovpn=saved["ovpn"], country=saved.get("country", server.country),
                             http_port=int(saved["http_port"]), socks_port=saved.get("socks_port"))
            self._register(spec, server)
            if st.get("desired", "running") == "running":
                try:
                    self.start(pid)
                    started.append(pid)
                except HmaProxyError as exc:
                    get_logger(pid).error(str(exc))
        return started

    def _register(self, spec: ProxySpec, server: Server) -> Instance:
        inst = self.instance_factory(spec, server)
        self.instances[spec.id] = inst
        self.specs[spec.id] = spec
        return inst

    def _save(self, spec: ProxySpec) -> None:
        with self.store.transaction() as data:
            st = self.store.proxy(data, spec.id)
            st["spec"] = {"ovpn": spec.ovpn, "country": spec.country, "http_port": spec.http_port,
                          "socks_port": spec.socks_port}
            st["ovpn"] = spec.ovpn

    # ------------------------------------------------------------------ lifecycle
    def create(self, spec: ProxySpec, server: Server, creds: Credentials | None = None, start: bool = True) -> InstanceInfo:
        if spec.id in self.instances:
            raise HmaProxyError(t("container_exists", id=spec.id, name=spec.id))
        self.preflight()
        if self.check_system and not self.cfg.app_mode:
            self.adapters.ensure(self.adapter_name(spec.id))
        spec = ProxySpec(id=spec.id, ovpn=server.filename, country=spec.country or server.country,
                         http_port=spec.http_port, socks_port=spec.socks_port)
        self._register(spec, server)
        self._save(spec)
        get_logger(spec.id).info(t("created", id=spec.id, country=spec.country, ovpn=server.filename,
                                   bind=self.cfg.bind_address, port=spec.http_port))
        if start:
            self.start(spec.id)
        return self.inspect(spec.id)

    def _require(self, proxy_id: str) -> Instance:
        inst = self.instances.get(proxy_id)
        if inst is None:
            raise NotFoundError(t("proxy_not_found", id=proxy_id))
        return inst

    def _listen(self, proxy_id: str) -> None:
        spec = self.specs[proxy_id]
        inst = self.instances[proxy_id]
        auth = Auth(self.cfg.proxy_auth.user, self.cfg.proxy_auth.password) if self.cfg.proxy_auth.enabled else None
        proxy = TunnelProxy(proxy_id, self.cfg.bind_address, spec.http_port, spec.socks_port,
                            source=lambda: inst.local_ip if inst.status == "connected" else None,
                            resolver=self.resolver, auth=auth)
        self.hub.add(proxy)

    def start(self, proxy_id: str) -> None:
        inst = self._require(proxy_id)
        self.preflight()
        if not self.hub.has(proxy_id):
            self._listen(proxy_id)
        try:
            inst.start()
        except OSError as exc:
            raise HmaProxyError(t("vpn_start_failed", error=exc)) from exc
        self.store.update_proxy(proxy_id, pid=inst.pid)

    def stop(self, proxy_id: str, timeout: int = 10) -> None:
        inst = self._require(proxy_id)
        self.hub.remove(proxy_id)
        inst.stop(timeout)
        self.store.update_proxy(proxy_id, pid=None)

    def restart(self, proxy_id: str, timeout: int = 10) -> None:
        inst = self._require(proxy_id)
        self.preflight()
        self._listen(proxy_id)  # re-open listeners so bind/auth changes apply
        inst.stop(timeout)
        try:
            inst.start()
        except OSError as exc:
            raise HmaProxyError(t("vpn_start_failed", error=exc)) from exc
        self.store.update_proxy(proxy_id, pid=inst.pid)

    def restart_socks(self, proxy_id: str) -> None:  # SOCKS5 is in-process; restarting the listener is enough
        if self.hub.has(proxy_id):
            self._listen(proxy_id)

    def remove(self, proxy_id: str) -> None:
        inst = self._require(proxy_id)
        self.hub.remove(proxy_id)
        inst.stop()
        self.instances.pop(proxy_id, None)
        self.specs.pop(proxy_id, None)
        if self.check_system and not self.cfg.app_mode:
            try:
                self.adapters.delete(self.adapter_name(proxy_id))
            except HmaProxyError as exc:
                get_logger(proxy_id).warning(str(exc))

    def switch_server(self, spec: ProxySpec, server: Server, creds: Credentials | None = None) -> InstanceInfo:
        """Same id and ports, another .ovpn."""
        old = self.instances.get(spec.id)
        if old is not None:
            old.stop()
        new_spec = ProxySpec(id=spec.id, ovpn=server.filename, country=spec.country, http_port=spec.http_port,
                             socks_port=spec.socks_port)
        self._register(new_spec, server)
        self._save(new_spec)
        self.hub.remove(spec.id)
        self.start(spec.id)
        return self.inspect(spec.id)

    def shutdown(self) -> None:
        """Stop every tunnel (program exit). Desired state is kept, so they come back next start."""
        for pid in list(self.instances):
            try:
                self.hub.remove(pid)
                self.instances[pid].stop(timeout=8)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ inspect
    def inspect(self, proxy_id: str) -> InstanceInfo:
        inst = self.instances.get(proxy_id)
        if inst is None:
            return InstanceInfo(id=proxy_id, status="missing")
        spec = self.specs[proxy_id]
        return InstanceInfo(
            id=proxy_id, status=inst.status, ovpn=spec.ovpn, country=spec.country,
            http_port=spec.http_port, socks_port=spec.socks_port, local_ip=inst.local_ip,
            remote_ip=inst.remote_ip, state_name=inst.state_name, started_at=inst.started_at,
            connected_at=inst.connected_at, last_error=inst.last_error, pid=inst.pid,
            socks_status=None,
        )

    def list_managed(self) -> dict[str, InstanceInfo]:
        return {pid: self.inspect(pid) for pid in sorted(self.instances)}

    def logs(self, proxy_id: str, tail: int = 200, **_) -> list[str]:
        inst = self._require(proxy_id)
        return list(inst.lines)[-tail:]


def uptime_seconds(connected_at: float | None, now: float | None = None) -> float | None:
    if not connected_at:
        return None
    return max(0.0, (now or time.time()) - connected_at)
