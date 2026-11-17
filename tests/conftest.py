from __future__ import annotations

import itertools
import sys
from pathlib import Path

import docker.errors
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hmaproxy import i18n  # noqa: E402

OVPN_FILES = [
    "Japan.Tokyo.TCP.ovpn", "Japan.Osaka.TCP.ovpn", "Singapore.TCP.ovpn",
    "USA.NewYork.UDP.ovpn", "USA.Dallas.TCP.ovpn", "Germany.Berlin.TCP.ovpn",
]
SECRET = "S3cr3t-HMA-pass!"


class FakeContainer:
    _ids = itertools.count(1)

    def __init__(self, client, image, name, labels=None, **kwargs):
        self.client = client
        self.image = image
        self.name = name
        self.labels = labels or {}
        self.kwargs = kwargs
        self.status = "created"
        self.id = f"c{next(self._ids)}"
        self.calls = []

    @property
    def attrs(self):
        running = self.status == "running"
        return {"State": {"Running": running, "StartedAt": "2026-01-01T00:00:00.123456789Z" if running else "0001-01-01T00:00:00Z"},
                "Config": {"Labels": self.labels}}

    def reload(self):
        pass

    def start(self):
        self.calls.append("start")
        self.status = "running"

    def stop(self, timeout=10):
        self.calls.append("stop")
        self.status = "exited"

    def restart(self, timeout=10):
        self.calls.append("restart")
        self.status = "running"

    def remove(self, force=False):
        self.calls.append("remove")
        self.client.containers.items.pop(self.name, None)

    def logs(self, tail=100, timestamps=False, stream=False, follow=False):
        text = f"log line for {self.name} password={SECRET}\n".encode()
        return iter([text]) if stream else text


class FakeContainers:
    def __init__(self, client):
        self.client = client
        self.items: dict[str, FakeContainer] = {}

    def create(self, image, name=None, labels=None, **kwargs):
        if name in self.items:
            raise docker.errors.APIError("Conflict")
        c = FakeContainer(self.client, image, name, labels, **kwargs)
        self.items[name] = c
        return c

    def get(self, name):
        try:
            return self.items[name]
        except KeyError:
            raise docker.errors.NotFound(f"No such container: {name}") from None

    def list(self, all=False, filters=None):
        wanted = dict(f.split("=", 1) for f in (filters or {}).get("label", []))
        return [c for c in self.items.values()
                if (all or c.status == "running") and _labels_match(c.labels, wanted)]


def _labels_match(labels, wanted):
    return all(labels.get(k) == v for k, v in wanted.items())


class FakeImages:
    def __init__(self):
        self.pulled = []
        self.present = set()

    def get(self, image):
        if image not in self.present:
            raise docker.errors.ImageNotFound(image)

    def pull(self, image):
        self.pulled.append(image)
        self.present.add(image)


class FakeDocker:
    def __init__(self):
        self.containers = FakeContainers(self)
        self.images = FakeImages()

    def ping(self):
        return True


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
            "base_port": 8881,
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
def fake_docker():
    return FakeDocker()


@pytest.fixture
def manager(write_config, fake_docker, monkeypatch):
    from hmaproxy import docker_ctl
    from hmaproxy.service import Manager
    from hmaproxy.docker_ctl import DockerController

    monkeypatch.setattr(docker_ctl.DockerController, "preflight", lambda self: None)

    def make(**overrides):
        path = write_config(**overrides)
        mgr = Manager.from_path(str(path))
        mgr.ctl = DockerController(mgr.cfg, client=fake_docker)
        return mgr

    return make
