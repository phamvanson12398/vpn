"""Create and control the gluetun (+ optional SOCKS5) containers via the Docker SDK.

Layout per proxy ``<id>``:

* ``<prefix>-<id>``        gluetun: OpenVPN client + kill switch + DNS + HTTP proxy (:8888).
                           All published ports live on this container.
* ``<prefix>-<id>-socks``  gost SOCKS5 server (:1080) sharing gluetun's network
                           namespace (``network_mode: container:<prefix>-<id>``), so
                           it can only reach the Internet through the VPN tunnel.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .config import AppConfig, ProxySpec
from .errors import DockerError, NotFoundError
from .i18n import t
from .logging_setup import get_logger, register_secret
from .providers import Credentials, Server

LABEL_APP = "hmaproxy.app"
LABEL_ID = "hmaproxy.id"
LABEL_ROLE = "hmaproxy.role"
LABEL_OVPN = "hmaproxy.ovpn"
LABEL_COUNTRY = "hmaproxy.country"
LABEL_HTTP_PORT = "hmaproxy.http_port"
LABEL_SOCKS_PORT = "hmaproxy.socks_port"
LABEL_PROVIDER = "hmaproxy.provider"

HTTP_PROXY_PORT = 8888
SOCKS_PORT = 1080
OVPN_IN_CONTAINER = "/gluetun/custom/config.ovpn"
USER_SECRET_IN_CONTAINER = "/run/secrets/openvpn_user"
PASS_SECRET_IN_CONTAINER = "/run/secrets/openvpn_password"


@dataclass
class ContainerInfo:
    id: str
    name: str
    status: str                      # running, exited, created, restarting, paused, dead, missing
    started_at: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    socks_status: str | None = None  # None when SOCKS5 is not used

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def ovpn(self) -> str:
        return self.labels.get(LABEL_OVPN, "")

    @property
    def country(self) -> str:
        return self.labels.get(LABEL_COUNTRY, "")

    @property
    def http_port(self) -> int | None:
        v = self.labels.get(LABEL_HTTP_PORT)
        return int(v) if v else None

    @property
    def socks_port(self) -> int | None:
        v = self.labels.get(LABEL_SOCKS_PORT)
        return int(v) if v else None


def host_path(path: Path) -> str:
    """Path string Docker understands for a bind mount.

    On Windows (Docker Desktop) ``C:\\x\\y`` is passed as ``C:/x/y``.
    """
    p = str(Path(path).resolve())
    if sys.platform == "win32":
        p = p.replace("\\", "/")
    return p


def _docker_module():
    try:
        import docker  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DockerError(t("docker_sdk_missing")) from exc
    return docker


class DockerController:
    def __init__(self, cfg: AppConfig, client: Any = None):
        self.cfg = cfg
        self._client = client
        self.log = get_logger()

    # ------------------------------------------------------------------ client
    @property
    def client(self):
        if self._client is None:
            docker = _docker_module()
            try:
                self._client = docker.from_env()
                self._client.ping()
            except Exception as exc:
                raise DockerError(t("docker_unavailable", error=exc)) from exc
        return self._client

    def _not_found_type(self):
        try:
            import docker.errors  # type: ignore

            return docker.errors.NotFound
        except ImportError:  # pragma: no cover
            return LookupError

    # ------------------------------------------------------------------ names
    def vpn_name(self, proxy_id: str) -> str:
        return f"{self.cfg.container_prefix}-{proxy_id}"

    def socks_name(self, proxy_id: str) -> str:
        return f"{self.cfg.container_prefix}-{proxy_id}-socks"

    def _get(self, name: str):
        try:
            return self.client.containers.get(name)
        except self._not_found_type():
            return None
        except DockerError:
            raise
        except Exception as exc:
            if exc.__class__.__name__ == "NotFound":
                return None
            raise DockerError(str(exc)) from exc

    # ------------------------------------------------------------------ files
    def _write_secrets(self, creds: Credentials) -> tuple[Path, Path]:
        register_secret(creds.password)
        secret_dir = self.cfg.state_dir / "secrets"
        secret_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(secret_dir, 0o700)
        user_file = secret_dir / f"{self.cfg.provider_name}_user"
        pass_file = secret_dir / f"{self.cfg.provider_name}_password"
        for path, value in ((user_file, creds.user), (pass_file, creds.password)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value)
            os.chmod(path, 0o600)
        return user_file, pass_file

    def _resolv_conf(self) -> Path:
        """resolv.conf for the SOCKS container: use gluetun's DNS on 127.0.0.1 (inside the VPN)."""
        path = self.cfg.state_dir / "resolv.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("nameserver 127.0.0.1\noptions ndots:0\n", encoding="utf-8")
        os.chmod(path, 0o644)
        return path

    # ------------------------------------------------------------------ preflight
    def preflight(self) -> None:
        # On Windows/macOS the TUN device lives inside Docker Desktop's VM, not on the host.
        if sys.platform.startswith("linux") and not Path("/dev/net/tun").exists():
            raise DockerError(t("tun_missing"))
        _ = self.client

    def ensure_image(self, image: str) -> None:
        try:
            self.client.images.get(image)
            return
        except Exception as exc:
            if exc.__class__.__name__ not in ("ImageNotFound", "NotFound"):
                raise DockerError(str(exc)) from exc
        self.log.info(t("pulling_image", image=image))
        try:
            self.client.images.pull(image)
        except Exception as exc:
            raise DockerError(str(exc)) from exc

    # ------------------------------------------------------------------ build
    def gluetun_env(self, spec: ProxySpec) -> dict[str, str]:
        env = dict(self.cfg.provider.gluetun_env(OVPN_IN_CONTAINER))
        env.update(
            {
                # Credentials are read from secret files, never put in the env
                # (env is visible in `docker inspect`).
                "OPENVPN_USER_SECRETFILE": USER_SECRET_IN_CONTAINER,
                "OPENVPN_PASSWORD_SECRETFILE": PASS_SECRET_IN_CONTAINER,
                "HTTPPROXY": "on",
                "HTTPPROXY_LISTENING_ADDRESS": f":{HTTP_PROXY_PORT}",
                "HTTPPROXY_STEALTH": "on",
                "HTTPPROXY_LOG": "off",
                # Kill switch is always on in gluetun; DNS goes through the tunnel.
                "DOT": "on",
                "TZ": os.environ.get("TZ", "UTC"),
            }
        )
        if self.cfg.proxy_auth.enabled:
            env["HTTPPROXY_USER"] = self.cfg.proxy_auth.user
            env["HTTPPROXY_PASSWORD"] = self.cfg.proxy_auth.password
        if spec.socks_port is not None:
            # Let gluetun's firewall accept inbound connections to the SOCKS port.
            env["FIREWALL_INPUT_PORTS"] = str(SOCKS_PORT)
        return env

    def _labels(self, spec: ProxySpec, server: Server, role: str) -> dict[str, str]:
        labels = {
            LABEL_APP: "1",
            LABEL_ID: spec.id,
            LABEL_ROLE: role,
            LABEL_OVPN: server.filename,
            LABEL_COUNTRY: spec.country or server.country,
            LABEL_HTTP_PORT: str(spec.http_port),
            LABEL_PROVIDER: self.cfg.provider_name,
        }
        if spec.socks_port is not None:
            labels[LABEL_SOCKS_PORT] = str(spec.socks_port)
        return labels

    def _socks_command(self) -> list[str]:
        if self.cfg.proxy_auth.enabled:
            user = quote(self.cfg.proxy_auth.user, safe="")
            pw = quote(self.cfg.proxy_auth.password, safe="")
            return ["-L", f"socks5://{user}:{pw}@:{SOCKS_PORT}"]
        return ["-L", f"socks5://:{SOCKS_PORT}"]

    def create(self, spec: ProxySpec, server: Server, creds: Credentials, start: bool = True) -> ContainerInfo:
        if self._get(self.vpn_name(spec.id)) is not None:
            raise DockerError(t("container_exists", id=spec.id, name=self.vpn_name(spec.id)))
        user_file, pass_file = self._write_secrets(creds)
        bind = self.cfg.bind_address
        ports: dict[str, Any] = {f"{HTTP_PROXY_PORT}/tcp": (bind, spec.http_port)}
        if spec.socks_port is not None:
            ports[f"{SOCKS_PORT}/tcp"] = (bind, spec.socks_port)
        volumes = {
            host_path(server.path): {"bind": OVPN_IN_CONTAINER, "mode": "ro"},
            host_path(user_file): {"bind": USER_SECRET_IN_CONTAINER, "mode": "ro"},
            host_path(pass_file): {"bind": PASS_SECRET_IN_CONTAINER, "mode": "ro"},
        }
        log = get_logger(spec.id)
        try:
            vpn = self.client.containers.create(
                self.cfg.gluetun_image,
                name=self.vpn_name(spec.id),
                cap_add=["NET_ADMIN"],
                devices=["/dev/net/tun:/dev/net/tun"],
                environment=self.gluetun_env(spec),
                volumes=volumes,
                ports=ports,
                labels=self._labels(spec, server, "vpn"),
                restart_policy={"Name": "unless-stopped"},
                log_config={"type": "json-file", "config": {"max-size": "10m", "max-file": "3"}},
            )
            if spec.socks_port is not None:
                self.client.containers.create(
                    self.cfg.socks5.image,
                    name=self.socks_name(spec.id),
                    command=self._socks_command(),
                    network_mode=f"container:{self.vpn_name(spec.id)}",
                    volumes={host_path(self._resolv_conf()): {"bind": "/etc/resolv.conf", "mode": "ro"}},
                    labels=self._labels(spec, server, "socks"),
                    restart_policy={"Name": "unless-stopped"},
                    log_config={"type": "json-file", "config": {"max-size": "5m", "max-file": "2"}},
                )
        except DockerError:
            raise
        except Exception as exc:
            self._remove_quiet(spec.id)
            raise DockerError(str(exc)) from exc
        log.info(t("created", id=spec.id, country=spec.country, ovpn=server.filename, bind=bind, port=spec.http_port))
        if start:
            self.start(spec.id)
        return self.inspect(spec.id)

    # ------------------------------------------------------------------ lifecycle
    def _require(self, proxy_id: str):
        c = self._get(self.vpn_name(proxy_id))
        if c is None:
            raise NotFoundError(t("proxy_not_found", id=proxy_id))
        return c

    def _socks(self, proxy_id: str):
        return self._get(self.socks_name(proxy_id))

    def start(self, proxy_id: str) -> None:
        vpn = self._require(proxy_id)
        try:
            vpn.start()
            socks = self._socks(proxy_id)
            if socks is not None:
                # The SOCKS container joins gluetun's netns; it must be (re)started
                # after gluetun so it attaches to the current namespace.
                socks.restart(timeout=5) if _status(socks) == "running" else socks.start()
        except Exception as exc:
            raise DockerError(str(exc)) from exc

    def stop(self, proxy_id: str, timeout: int = 10) -> None:
        vpn = self._require(proxy_id)
        try:
            socks = self._socks(proxy_id)
            if socks is not None:
                socks.stop(timeout=timeout)
            vpn.stop(timeout=timeout)
        except Exception as exc:
            raise DockerError(str(exc)) from exc

    def restart(self, proxy_id: str, timeout: int = 10) -> None:
        vpn = self._require(proxy_id)
        try:
            vpn.restart(timeout=timeout)
            socks = self._socks(proxy_id)
            if socks is not None:
                socks.restart(timeout=5)
        except Exception as exc:
            raise DockerError(str(exc)) from exc

    def restart_socks(self, proxy_id: str) -> None:
        socks = self._socks(proxy_id)
        if socks is None:
            return
        try:
            socks.restart(timeout=5)
        except Exception as exc:
            raise DockerError(str(exc)) from exc

    def remove(self, proxy_id: str) -> None:
        self._require(proxy_id)
        self._remove_quiet(proxy_id, strict=True)

    def _remove_quiet(self, proxy_id: str, strict: bool = False) -> None:
        for name in (self.socks_name(proxy_id), self.vpn_name(proxy_id)):
            c = self._get(name)
            if c is None:
                continue
            try:
                c.remove(force=True)
            except Exception as exc:
                if strict:
                    raise DockerError(str(exc)) from exc

    def switch_server(self, spec: ProxySpec, server: Server, creds: Credentials) -> ContainerInfo:
        """Recreate the proxy with another .ovpn file, keeping id and ports."""
        self._remove_quiet(spec.id)
        return self.create(spec, server, creds, start=True)

    # ------------------------------------------------------------------ inspect
    def inspect(self, proxy_id: str) -> ContainerInfo:
        c = self._get(self.vpn_name(proxy_id))
        if c is None:
            return ContainerInfo(id=proxy_id, name=self.vpn_name(proxy_id), status="missing")
        return self._info(c)

    def _info(self, c) -> ContainerInfo:
        labels = dict(getattr(c, "labels", None) or c.attrs.get("Config", {}).get("Labels") or {})
        proxy_id = labels.get(LABEL_ID, c.name)
        socks_status = None
        if LABEL_SOCKS_PORT in labels:
            socks = self._socks(proxy_id)
            socks_status = _status(socks) if socks is not None else "missing"
        state = (c.attrs or {}).get("State", {})
        started = state.get("StartedAt") if state.get("Running") else None
        return ContainerInfo(
            id=proxy_id,
            name=c.name,
            status=_status(c),
            started_at=started,
            labels=labels,
            socks_status=socks_status,
        )

    def list_managed(self) -> dict[str, ContainerInfo]:
        """All VPN containers created by this tool (any state), keyed by proxy id."""
        try:
            containers = self.client.containers.list(
                all=True, filters={"label": [f"{LABEL_APP}=1", f"{LABEL_ROLE}=vpn"]}
            )
        except DockerError:
            raise
        except Exception as exc:
            raise DockerError(str(exc)) from exc
        prefix = f"{self.cfg.container_prefix}-"
        out: dict[str, ContainerInfo] = {}
        for c in containers:
            if not c.name.startswith(prefix):
                continue
            info = self._info(c)
            out[info.id] = info
        return out

    def logs(self, proxy_id: str, tail: int = 100, follow: bool = False, socks: bool = False) -> Iterator[str]:
        c = self._socks(proxy_id) if socks else self._require(proxy_id)
        if c is None:
            raise NotFoundError(t("proxy_not_found", id=f"{proxy_id}-socks"))
        try:
            if follow:
                for chunk in c.logs(stream=True, follow=True, tail=tail, timestamps=True):
                    yield chunk.decode("utf-8", "replace")
            else:
                yield c.logs(tail=tail, timestamps=True).decode("utf-8", "replace")
        except Exception as exc:
            raise DockerError(str(exc)) from exc


def _status(container) -> str:
    try:
        container.reload()
    except Exception:
        pass
    return getattr(container, "status", "unknown")


def uptime_seconds(started_at: str | None, now: float | None = None) -> float | None:
    """Parse Docker's RFC3339Nano StartedAt and return seconds since then."""
    if not started_at or started_at.startswith("0001-"):
        return None
    from datetime import datetime, timezone

    s = started_at.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        frac = "".join(ch for ch in frac if ch.isdigit())[:6]
        s = f"{head}.{frac}" if frac else head
    try:
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (now or time.time()) - dt.timestamp())
