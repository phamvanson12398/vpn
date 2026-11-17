"""Load and validate config.yaml and the .env credentials file (FR-1)."""

from __future__ import annotations

import ipaddress
import os
import random
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, CredentialError
from .i18n import t
from .providers import Credentials, Server, VpnProvider, get_provider, normalize_country

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DRIVERS = ("tap-windows6", "wintun", "ovpn-dco")


@dataclass
class HealthConfig:
    interval_seconds: int = 60
    failure_threshold: int = 3
    ip_check_url: str = "https://api.ipify.org"
    fallback_urls: list[str] = field(default_factory=lambda: ["https://ifconfig.me/ip", "https://icanhazip.com"])
    timeout_seconds: float = 15.0
    startup_grace_seconds: int = 90


@dataclass
class RecoveryConfig:
    max_restarts: int = 3
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 900
    switch_server: bool = True
    max_server_switches: int = 3


@dataclass
class ProxyAuth:
    enabled: bool = False
    user: str = ""
    password: str = ""


@dataclass
class Socks5Config:
    enabled: bool = False
    base_port: int = 1081


@dataclass
class OpenVPNConfig:
    path: str = ""                    # empty = auto-detect C:\Program Files\OpenVPN\bin\openvpn.exe
    driver: str = "tap-windows6"      # tap-windows6 | wintun | ovpn-dco
    adapter_prefix: str = "HMA-Proxy"
    route_metric: int = 9000
    verbosity: int = 3
    extra_config: list[str] = field(default_factory=list)  # extra lines appended to every .ovpn
    connect_timeout_seconds: int = 60


@dataclass
class DnsConfig:
    servers: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8", "9.9.9.9"])
    timeout_seconds: float = 5.0
    cache_seconds: int = 300


@dataclass
class GatewayConfig:
    enabled: bool = False
    http_port: int = 8880
    socks_port: int = 1080
    mode: str = "roundrobin"  # roundrobin | random


@dataclass
class WebConfig:
    bind: str = "127.0.0.1"
    port: int = 8800
    password: str = ""   # set to require HTTP Basic auth (user: admin)
    open_browser: bool = True


@dataclass
class LogConfig:
    file: str = "logs/hmaproxy.log"  # relative to state_dir
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5


@dataclass
class SelectionConfig:
    """``selection:`` block - pick servers automatically instead of listing them."""

    mode: str = "list"  # list | random
    count: int = 0
    distinct_countries: bool = True
    countries: list[str] = field(default_factory=list)
    protocol: str = ""  # "", tcp, udp


@dataclass
class ProxySpec:
    id: str
    ovpn: str
    country: str
    http_port: int
    socks_port: int | None = None


@dataclass
class AppConfig:
    path: Path
    base_dir: Path
    provider_name: str
    provider: VpnProvider
    max_connections: int
    ovpn_dir: Path
    bind_address: str
    base_port: int
    state_dir: Path
    env_file: Path
    language: str
    openvpn: OpenVPNConfig
    dns: DnsConfig
    health: HealthConfig
    recovery: RecoveryConfig
    proxy_auth: ProxyAuth
    socks5: Socks5Config
    gateway: GatewayConfig
    web: WebConfig
    log: LogConfig
    selection: SelectionConfig
    raw_proxies: list[dict[str, Any]]
    servers: list[Server]
    proxies: list[ProxySpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def server_by_file(self, filename: str) -> Server | None:
        for s in self.servers:
            if s.filename == filename:
                return s
        return None

    def proxy(self, proxy_id: str) -> ProxySpec | None:
        for p in self.proxies:
            if p.id == proxy_id:
                return p
        return None

    @property
    def is_public_bind(self) -> bool:
        return self.bind_address not in ("127.0.0.1", "localhost", "::1")

    @property
    def connect_host(self) -> str:
        """Address the manager itself uses to reach the published ports."""
        if self.bind_address in ("0.0.0.0", "::", ""):
            return "127.0.0.1"
        return self.bind_address


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _section(data: dict, name: str) -> dict:
    value = data.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(t("config_field_invalid", field=name, reason="must be a mapping"))
    return value


def _build(cls, data: dict, section: str):
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(t("config_field_invalid", field=f"{section}.{sorted(unknown)[0]}", reason="unknown key"))
    try:
        obj = cls(**data)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ConfigError(t("config_field_invalid", field=section, reason=str(exc))) from exc
    for name, f in cls.__dataclass_fields__.items():
        value = getattr(obj, name)
        default = f.default
        if isinstance(default, str) and not isinstance(value, str):
            raise ConfigError(t("config_field_invalid", field=f"{section}.{name}", reason="must be a string"))
        if isinstance(default, bool) and not isinstance(value, bool):
            raise ConfigError(t("config_field_invalid", field=f"{section}.{name}", reason="must be true/false"))
        if isinstance(default, int) and not isinstance(default, bool):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(t("config_field_invalid", field=f"{section}.{name}", reason="must be a number"))
    return obj


def _resolve_path(base: Path, value: str | os.PathLike) -> Path:
    p = Path(os.path.expanduser(str(value)))
    return p if p.is_absolute() else (base / p).resolve()


def _check_port(port: Any, owner: str) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(t("port_range", port=port, id=owner))
    return port


# ----------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------
def load_config(path: str | os.PathLike = "config.yaml", saved_plan: list[dict] | None = None) -> AppConfig:
    """Read config.yaml, validate everything and resolve the list of proxies.

    ``saved_plan`` is the list of proxies chosen by a previous ``create`` in
    random-selection mode, so that later commands see the same servers.
    """
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(t("config_not_found", path=cfg_path))
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(t("config_invalid_yaml", error=exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError(t("config_invalid_yaml", error="top level must be a mapping"))

    base = cfg_path.parent
    provider_name = str(data.get("provider", "hma"))
    provider = get_provider(provider_name)

    max_conn = data.get("max_connections", 5)
    if isinstance(max_conn, bool) or not isinstance(max_conn, int) or max_conn < 1:
        raise ConfigError(t("config_field_invalid", field="max_connections", reason="must be a positive integer"))

    bind = str(data.get("bind_address", "127.0.0.1"))
    if bind != "localhost":
        try:
            ipaddress.ip_address(bind)
        except ValueError as exc:
            raise ConfigError(t("config_field_invalid", field="bind_address", reason=str(exc))) from exc

    base_port = _check_port(data.get("base_port", 8881), "base_port")

    socks = _build(Socks5Config, _section(data, "socks5"), "socks5")

    cfg = AppConfig(
        path=cfg_path,
        base_dir=base,
        provider_name=provider.name,
        provider=provider,
        max_connections=max_conn,
        ovpn_dir=_resolve_path(base, data.get("ovpn_dir", "./ovpn")),
        bind_address=bind,
        base_port=base_port,
        state_dir=_resolve_path(base, data.get("state_dir", "./.hmaproxy")),
        env_file=_resolve_path(base, data.get("env_file", ".env")),
        language=str(data.get("language", "vi")),
        openvpn=_build(OpenVPNConfig, _section(data, "openvpn"), "openvpn"),
        dns=_build(DnsConfig, _section(data, "dns"), "dns"),
        health=_build(HealthConfig, _section(data, "health_check"), "health_check"),
        recovery=_build(RecoveryConfig, _section(data, "recovery"), "recovery"),
        proxy_auth=_build(ProxyAuth, _section(data, "proxy_auth"), "proxy_auth"),
        socks5=socks,
        gateway=_build(GatewayConfig, _section(data, "gateway"), "gateway"),
        web=_build(WebConfig, _section(data, "web"), "web"),
        log=_build(LogConfig, _section(data, "logging"), "logging"),
        selection=_build(SelectionConfig, _section(data, "selection"), "selection"),
        raw_proxies=list(data.get("proxies") or []),
        servers=[],
    )
    if cfg.openvpn.driver not in DRIVERS:
        raise ConfigError(t("config_field_invalid", field="openvpn.driver", reason=" | ".join(DRIVERS)))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,20}", cfg.openvpn.adapter_prefix):
        raise ConfigError(t("config_field_invalid", field="openvpn.adapter_prefix", reason="letters, digits, - _"))
    for server in cfg.dns.servers:
        try:
            ipaddress.IPv4Address(str(server))
        except ValueError as exc:
            raise ConfigError(t("config_field_invalid", field="dns.servers", reason=str(exc))) from exc
    if not cfg.dns.servers:
        raise ConfigError(t("config_field_invalid", field="dns.servers", reason="at least one IPv4 DNS server"))
    _validate_scalars(cfg)

    if not cfg.ovpn_dir.is_dir():
        raise ConfigError(t("ovpn_dir_missing", path=cfg.ovpn_dir))
    cfg.servers = provider.list_servers(cfg.ovpn_dir)
    if not cfg.servers:
        # Normal on first run: not an error, the UI guides the user to add files.
        cfg.warnings.append(t("ovpn_dir_empty", path=cfg.ovpn_dir))

    cfg.proxies = _resolve_proxies(cfg, saved_plan)
    _validate_proxies(cfg)

    if cfg.is_public_bind and not cfg.proxy_auth.enabled:
        cfg.warnings.append(t("public_bind_no_auth", bind=cfg.bind_address))
    return cfg


def _validate_scalars(cfg: AppConfig) -> None:
    h = cfg.health
    if h.interval_seconds < 5:
        raise ConfigError(t("config_field_invalid", field="health_check.interval_seconds", reason=">= 5"))
    if h.failure_threshold < 1:
        raise ConfigError(t("config_field_invalid", field="health_check.failure_threshold", reason=">= 1"))
    if not str(h.ip_check_url).startswith(("http://", "https://")):
        raise ConfigError(t("config_field_invalid", field="health_check.ip_check_url", reason="must be an http(s) URL"))
    r = cfg.recovery
    if r.max_restarts < 0 or r.backoff_base_seconds < 0 or r.backoff_max_seconds < r.backoff_base_seconds:
        raise ConfigError(t("config_field_invalid", field="recovery", reason="negative values or max < base"))
    if cfg.proxy_auth.enabled and not (cfg.proxy_auth.user and cfg.proxy_auth.password):
        raise ConfigError(t("auth_incomplete"))
    if cfg.proxy_auth.enabled and (":" in cfg.proxy_auth.user or "@" in cfg.proxy_auth.user):
        raise ConfigError(t("config_field_invalid", field="proxy_auth.user", reason="must not contain ':' or '@'"))
    if cfg.gateway.mode not in ("roundrobin", "random"):
        raise ConfigError(t("config_field_invalid", field="gateway.mode", reason="roundrobin | random"))
    if cfg.selection.mode not in ("list", "random"):
        raise ConfigError(t("config_field_invalid", field="selection.mode", reason="list | random"))
    if cfg.selection.protocol not in ("", "tcp", "udp"):
        raise ConfigError(t("config_field_invalid", field="selection.protocol", reason="tcp | udp"))
    _check_port(cfg.socks5.base_port, "socks5.base_port")
    if cfg.log.level.upper() not in ("DEBUG", "INFO", "WARNING", "WARN", "ERROR"):
        raise ConfigError(t("config_field_invalid", field="logging.level", reason="DEBUG|INFO|WARNING|ERROR"))
    if cfg.language not in ("vi", "en"):
        raise ConfigError(t("config_field_invalid", field="language", reason="vi | en"))


def _resolve_proxies(cfg: AppConfig, saved_plan: list[dict] | None) -> list[ProxySpec]:
    entries: list[dict[str, Any]]
    if cfg.selection.mode == "random":
        if saved_plan:
            entries = [dict(e) for e in saved_plan]
        else:
            entries = pick_random_servers(cfg)
    else:
        entries = []
        for i, raw in enumerate(cfg.raw_proxies):
            if not isinstance(raw, dict) or "ovpn" not in raw:
                raise ConfigError(t("config_field_invalid", field=f"proxies[{i}]", reason="needs 'id' and 'ovpn'"))
            entries.append(dict(raw))

    if not entries:
        # Valid but empty: lets a new user add proxies from the web UI step by step.
        cfg.warnings.append(t("no_proxies"))
        return []
    # Rejected up front so the user gets the clear message even if other fields are wrong.
    if len(entries) > cfg.max_connections:
        raise ConfigError(t("too_many_proxies", count=len(entries), max=cfg.max_connections))

    specs: list[ProxySpec] = []
    for i, e in enumerate(entries):
        pid = str(e.get("id") or f"p{i + 1}").lower()
        ovpn = str(e["ovpn"])
        server = cfg.server_by_file(ovpn)
        if server is None:
            raise ConfigError(t("ovpn_missing", id=pid, ovpn=ovpn, dir=cfg.ovpn_dir))
        http_port = e.get("port", cfg.base_port + i)
        socks_port = None
        if cfg.socks5.enabled:
            socks_port = e.get("socks_port", cfg.socks5.base_port + i)
        specs.append(
            ProxySpec(
                id=pid,
                ovpn=ovpn,
                country=str(e.get("country") or server.country),
                http_port=http_port,
                socks_port=socks_port,
            )
        )
    return specs


def pick_random_servers(cfg: AppConfig, rng: random.Random | None = None) -> list[dict[str, Any]]:
    """Choose ``selection.count`` servers, by default each from a different country."""
    sel = cfg.selection
    rng = rng or random.SystemRandom()
    count = sel.count or cfg.max_connections
    if count > cfg.max_connections:
        raise ConfigError(t("too_many_proxies", count=count, max=cfg.max_connections))
    pool = list(cfg.servers)
    if sel.countries:
        wanted = {normalize_country(c) for c in sel.countries}
        pool = [s for s in pool if s.country_key in wanted]
    if sel.protocol:
        pool = [s for s in pool if s.protocol == sel.protocol]
    rng.shuffle(pool)
    chosen: list[Server] = []
    seen: set[str] = set()
    for s in pool:
        if sel.distinct_countries and s.country_key in seen:
            continue
        chosen.append(s)
        seen.add(s.country_key)
        if len(chosen) == count:
            break
    if len(chosen) < count:
        raise ConfigError(t("not_enough_servers", available=len(chosen), count=count))
    out = []
    used_ids: set[str] = set()
    for s in chosen:
        base_id = re.sub(r"[^a-z0-9]+", "-", s.country.lower()).strip("-")[:28] or "p"
        pid, n = base_id, 2
        while pid in used_ids:
            pid, n = f"{base_id}-{n}", n + 1
        used_ids.add(pid)
        out.append({"id": pid, "ovpn": s.filename, "country": s.country})
    return out


def _validate_proxies(cfg: AppConfig) -> None:
    ids: set[str] = set()
    ports: dict[int, str] = {}

    def claim(port: int, owner: str) -> None:
        _check_port(port, owner)
        if port in ports:
            raise ConfigError(t("duplicate_port", port=port, a=ports[port], b=owner))
        ports[port] = owner

    for p in cfg.proxies:
        if not ID_RE.match(p.id):
            raise ConfigError(t("bad_id", id=p.id))
        if p.id in ids:
            raise ConfigError(t("duplicate_id", id=p.id))
        ids.add(p.id)
        claim(p.http_port, f"{p.id}/http")
        if p.socks_port is not None:
            claim(p.socks_port, f"{p.id}/socks5")
    if cfg.gateway.enabled:
        claim(cfg.gateway.http_port, "gateway/http")
        if cfg.socks5.enabled:
            claim(cfg.gateway.socks_port, "gateway/socks5")
    claim(cfg.web.port, "web")


# ----------------------------------------------------------------------
# credentials
# ----------------------------------------------------------------------
def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def env_file_warning(path: Path) -> str | None:
    """Return a warning if the .env file is readable by group/others (POSIX only)."""
    if os.name == "nt":
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None
    if mode & 0o077:
        return t("env_perms", path=path, mode=oct(mode)[2:])
    return None


def load_credentials(cfg: AppConfig) -> Credentials:
    """Credentials from the environment first, then from the .env file."""
    file_values = parse_env_file(cfg.env_file)
    prov = cfg.provider
    user = os.environ.get(prov.user_env) or file_values.get(prov.user_env, "")
    password = os.environ.get(prov.pass_env) or file_values.get(prov.pass_env, "")
    if not user or not password:
        raise CredentialError(t("missing_credentials", names=f"{prov.user_env}, {prov.pass_env}"))
    return Credentials(user=user, password=password)
