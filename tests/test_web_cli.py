import json
import threading
import urllib.error
import urllib.request

import pytest

from hmaproxy import cli, docker_ctl
from tests.conftest import SECRET


@pytest.fixture
def web(write_config, fake_docker, monkeypatch):
    from hmaproxy import web as webmod

    monkeypatch.setattr(docker_ctl.DockerController, "preflight", lambda self: None)
    orig_init = docker_ctl.DockerController.__init__

    def init(self, cfg, client=None):
        orig_init(self, cfg, client=fake_docker)

    monkeypatch.setattr(docker_ctl.DockerController, "__init__", init)
    monkeypatch.setattr(webmod.Monitor, "run_in_thread", lambda self: None)
    monkeypatch.setattr("hmaproxy.service.Manager.real_ip", lambda self, refresh=False: "9.9.9.9")
    path = write_config()
    app = webmod.WebApp(path)
    server = webmod.make_server(app, "127.0.0.1", 0)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, url, body=None, headers=None):
        h = {"X-HMA-Request": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(base + url, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if "json" in resp.headers["Content-Type"] else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            return exc.code, (json.loads(raw) if raw.startswith("{") else raw)

    yield app, call
    server.shutdown()


def test_web_index_and_status(web):
    app, call = web
    code, html = call("GET", "/")
    assert code == 200 and "HMA Proxy Manager" in html
    code, st = call("GET", "/api/status")
    assert code == 200 and st["config_error"] is None and st["docker"]["ok"]
    assert [p["id"] for p in st["proxies"]] == ["jp", "sg", "us"]
    assert st["credentials_set"] and SECRET not in json.dumps(st)
    assert len(st["ovpn_files"]) == 6


def test_web_create_and_actions(web):
    app, call = web
    code, res = call("POST", "/api/create", {})
    assert code == 200 and res["ok"], res
    code, st = call("GET", "/api/status")
    assert all(p["status"] == "running" for p in st["proxies"])
    code, res = call("POST", "/api/proxies/jp/stop", {})
    assert res["ok"]
    code, res = call("POST", "/api/proxies/nope/start", {})
    assert code == 404
    code, out = call("GET", "/api/export?format=http&only_healthy=0&host=10.0.0.5")
    assert "http://10.0.0.5:8881" in out
    code, logs = call("GET", "/api/proxies/jp/logs?tail=50")
    assert code == 200 and SECRET not in logs


def test_web_requires_csrf_header_and_local_host(web):
    app, call = web
    code, _ = call("POST", "/api/create", {}, headers={"X-HMA-Request": "0"})
    assert code == 403
    code, _ = call("GET", "/api/status", headers={"Host": "evil.example:8800"})
    assert code == 403


def test_web_credentials_are_write_only(web, project):
    app, call = web
    code, res = call("POST", "/api/credentials", {"user": "newuser", "password": "Another#Pass 1"})
    assert res["ok"]
    env = (project / ".env").read_text()
    assert "HMA_USER=newuser" in env and "Another#Pass 1" in env
    code, st = call("GET", "/api/status")
    assert "Another#Pass 1" not in json.dumps(st) and st["vpn_user"] == "newuser"


def test_web_config_edit_and_ovpn_upload(web, project):
    app, call = web
    code, res = call("POST", "/api/ovpn", {"filename": "../evil.ovpn", "content": "remote 1.1.1.1"})
    assert res["ok"] and (project / "ovpn" / "evil.ovpn").exists()  # path stripped to basename
    code, res = call("POST", "/api/ovpn", {"filename": "x.txt", "content": "remote 1.1.1.1"})
    assert not res["ok"]
    code, res = call("POST", "/api/config/proxies", {"id": "de", "ovpn": "Germany.Berlin.TCP.ovpn"})
    assert res["ok"], res
    assert app.mgr.cfg.proxy("de").http_port == 8884
    code, res = call("POST", "/api/config", {"yaml": "max_connections: 1\nproxies:\n- {id: a, ovpn: Japan.Tokyo.TCP.ovpn}\n- {id: b, ovpn: Singapore.TCP.ovpn}\n"})
    assert not res["ok"] and "max_connections" in res["error"]
    code, st = call("GET", "/api/status")
    assert st["config_error"]


def test_web_starts_with_broken_config(tmp_path, monkeypatch):
    from hmaproxy import web as webmod

    monkeypatch.setattr(webmod.Monitor, "run_in_thread", lambda self: None)
    app = webmod.WebApp(tmp_path / "config.yaml")  # creates template, ovpn dir empty
    assert (tmp_path / "config.yaml").exists() and (tmp_path / ".env").exists()
    assert app.config_error and "ovpn" in app.config_error
    assert app.status()["credentials_set"] is False


# ---------------------------------------------------------------- CLI
def _patch_cli(monkeypatch, fake_docker):
    monkeypatch.setattr(docker_ctl.DockerController, "preflight", lambda self: None)
    orig_init = docker_ctl.DockerController.__init__
    monkeypatch.setattr(docker_ctl.DockerController, "__init__",
                        lambda self, cfg, client=None: orig_init(self, cfg, client=fake_docker))


def test_cli_rejects_over_limit(write_config, capsys):
    path = write_config(max_connections=2)
    assert cli.main(["--lang", "en", "-c", str(path), "validate"]) == 2
    assert "max_connections = 2" in capsys.readouterr().err


def test_cli_create_list_export(write_config, fake_docker, monkeypatch, capsys):
    _patch_cli(monkeypatch, fake_docker)
    path = str(write_config())
    assert cli.main(["-c", path, "create"]) == 0
    assert cli.main(["-c", path, "list"]) == 0
    out = capsys.readouterr()
    assert "jp" in out.out and "running" in out.out
    assert SECRET not in out.out + out.err
    assert cli.main(["-c", path, "export", "--format", "plain", "--all", "--host", "127.0.0.1"]) == 0
    assert capsys.readouterr().out.splitlines() == ["127.0.0.1:8881", "127.0.0.1:8882", "127.0.0.1:8883"]
    assert cli.main(["-c", path, "export", "--format", "socks5"]) == 2
    assert cli.main(["-c", path, "stop", "zz"]) == 5
    assert cli.main(["-c", path, "remove", "all"]) == 0
    assert fake_docker.containers.items == {}


def test_cli_missing_credentials_exit_code(write_config, project, fake_docker, monkeypatch):
    _patch_cli(monkeypatch, fake_docker)
    (project / ".env").write_text("")
    assert cli.main(["-c", str(write_config()), "create"]) == 4
