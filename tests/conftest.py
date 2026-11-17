from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hmaproxy import i18n  # noqa: E402

OVPN_FILES = [
    "Japan.Tokyo.TCP.ovpn", "Japan.Osaka.TCP.ovpn", "Singapore.TCP.ovpn",
    "USA.NewYork.UDP.ovpn", "USA.Dallas.TCP.ovpn", "Germany.Berlin.TCP.ovpn",
]
SECRET = "S3cr3t-HMA-pass!"


class FakeInstance:
    """Stands in for OpenVPNInstance: 'connects' instantly with tunnel IP 127.0.0.1."""

    def __init__(self, spec, server):
        self.id = spec.id
        self.server = server
        self.status = "stopped"
        self.state_name = ""
        self.local_ip = None
        self.remote_ip = None
        self.started_at = None
        self.connected_at = None
        self.last_error = None
        self.lines = [f"connecting with password={SECRET}"]
        self.calls = []
        self._pid = None

    @property
    def pid(self):
        return self._pid

    def start(self):
        self.calls.append("start")
        self.status = "connected"
        self.state_name = "CONNECTED"
        self.local_ip = "127.0.0.1"
        self.remote_ip = "203.0.113.9"
        self.started_at = self.connected_at = time.time()
        self._pid = 4242

    def stop(self, timeout=10.0):
        self.calls.append("stop")
        self.status = "stopped"
        self.local_ip = None
        self.connected_at = None

    def restart(self):
        self.stop()
        self.start()

    def crash(self):
        self.status = "exited"
        self.local_ip = None


def free_base_port(count=12):
    import socket

    for _ in range(50):
        base = random.randint(20000, 50000)
        ok = True
        for port in range(base, base + count):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    ok = False
                    break
        if ok:
            return base
    raise RuntimeError("no free ports")


@pytest.fixture(autouse=True)
def _lang(monkeypatch):
    i18n.set_language("en")
    monkeypatch.delenv("HMA_USER", raising=False)
    monkeypatch.delenv("HMA_PASS", raising=False)
    yield
    i18n.set_language("vi")


@pytest.fixture
def project(tmp_path):
    ovpn = tmp_path / "ovpn"
    ovpn.mkdir()
    for name in OVPN_FILES:
        (ovpn / name).write_text("client\ndev tun\nremote 10.0.0.1 443\nauth-user-pass\n")
    (tmp_path / ".env").write_text(f"HMA_USER=vpnuser\nHMA_PASS={SECRET}\n")
    (tmp_path / ".env").chmod(0o600)
    return tmp_path


@pytest.fixture
def write_config(project):
    def _write(**overrides):
        data = {
            "provider": "hma",
            "max_connections": 5,
            "ovpn_dir": "./ovpn",
            "bind_address": "127.0.0.1",
            "base_port": free_base_port(),
            "language": "en",
            "health_check": {"interval_seconds": 60, "failure_threshold": 3, "startup_grace_seconds": 0},
            "recovery": {"max_restarts": 2, "backoff_base_seconds": 30, "backoff_max_seconds": 600},
            "proxies": [
                {"id": "jp", "ovpn": "Japan.Tokyo.TCP.ovpn"},
                {"id": "sg", "ovpn": "Singapore.TCP.ovpn"},
                {"id": "us", "ovpn": "USA.NewYork.UDP.ovpn"},
            ],
        }
        for k, v in overrides.items():
            if v is None:
                data.pop(k, None)
            elif isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k] = {**data[k], **v}
            else:
                data[k] = v
        path = project / "config.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    return _write


@pytest.fixture
def instances():
    """All FakeInstances created during a test, by proxy id (latest wins)."""
    return {}


@pytest.fixture
def hub():
    from hmaproxy.proxy_server import ProxyHub

    h = ProxyHub()
    yield h
    h.stop()


@pytest.fixture
def make_ctl(instances, hub):
    from hmaproxy.config import load_credentials
    from hmaproxy.state import StateStore
    from hmaproxy.vpn_ctl import VpnController

    def make(cfg, store=None):
        def factory(spec, server):
            inst = FakeInstance(spec, server)
            instances[spec.id] = inst
            return inst

        return VpnController(cfg, store or StateStore(cfg.state_dir), credentials=lambda: load_credentials(cfg),
                             hub=hub, instance_factory=factory, check_system=False)

    return make


@pytest.fixture
def manager(write_config, make_ctl):
    from hmaproxy.config import load_config
    from hmaproxy.service import Manager

    def make(**overrides):
        cfg = load_config(write_config(**overrides))
        ctl = make_ctl(cfg)
        return Manager(cfg, ctl=ctl, store=ctl.store)

    return make
