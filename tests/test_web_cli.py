import json
import threading
import urllib.error
import urllib.request

import pytest

from hmaproxy import cli
from tests.conftest import SECRET, free_base_port


@pytest.fixture
def web(write_config, make_ctl, monkeypatch):
    from hmaproxy import web as webmod

    monkeypatch.setattr(webmod, "VpnController", lambda cfg, store, credentials: make_ctl(cfg, store))
    monkeypatch.setattr(webmod.Monitor, "run_in_thread", lambda self: None)
    monkeypatch.setattr("hmaproxy.service.Manager.real_ip", lambda self, refresh=False: "9.9.9.9")
    web_port = free_base_port(1)
    path = write_config(web={"port": web_port})
    app = webmod.WebApp(path)
    server = webmod.make_server(app, "127.0.0.1", web_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{web_port}"

    def call(method, url, body=None, headers=None):
        h = {"X-HMA-Request": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(base + url, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if "json" in resp.headers["Content-Type"] else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            return exc.code, (json.loads(raw) if raw.startswith("{") else raw)

    yield app, call, path
    server.shutdown()
    app.shutdown()


def test_web_index_and_status(web):
    app, call, _ = web
    code, html = call("GET", "/")
    assert code == 200 and "HMA Proxy Manager" in html
    code, st = call("GET", "/api/status")
    assert code == 200 and st["config_error"] is None
    assert [p["id"] for p in st["proxies"]] == ["jp", "sg", "us"]
    assert all(p["status"] == "not created" for p in st["proxies"])
    assert st["credentials_set"] and SECRET not in json.dumps(st)
    assert len(st["ovpn_files"]) == 6 and "system" in st


def test_web_create_and_actions(web):
    app, call, _ = web
    code, res = call("POST", "/api/create", {})
    assert code == 200 and res["ok"], res
    code, st = call("GET", "/api/status")
    assert all(p["status"] == "connected" and p["tunnel_ip"] == "127.0.0.1" for p in st["proxies"])
    code, res = call("POST", "/api/proxies/jp/stop", {})
    assert res["ok"]
    code, res = call("POST", "/api/proxies/nope/start", {})
    assert code == 404
    port = app.mgr.cfg.proxy("jp").http_port
    code, out = call("GET", "/api/export?format=http&only_healthy=0&host=10.0.0.5")
    assert f"http://10.0.0.5:{port}" in out
    code, logs = call("GET", "/api/proxies/jp/logs?tail=50")
    assert code == 200 and SECRET not in logs and "***" in logs
    code, res = call("POST", "/api/proxies/all/remove", {})
    assert res["ok"] and app.ctl.instances == {}


def test_web_requires_csrf_header_and_local_host(web):
    app, call, _ = web
    code, _ = call("POST", "/api/create", {}, headers={"X-HMA-Request": "0"})
    assert code == 403
    code, _ = call("GET", "/api/status", headers={"Host": "evil.example:8800"})
    assert code == 403


def test_web_credentials_are_write_only(web, project):
    app, call, _ = web
    code, res = call("POST", "/api/credentials", {"user": "newuser", "password": "Another#Pass 1"})
    assert res["ok"]
    env = (project / ".env").read_text()
    assert "HMA_USER=newuser" in env and "Another#Pass 1" in env
    code, st = call("GET", "/api/status")
    assert "Another#Pass 1" not in json.dumps(st) and st["vpn_user"] == "newuser"


def test_web_config_reload_keeps_running_tunnels(web, project):
    app, call, _ = web
    call("POST", "/api/create", {})
    ctl_before = app.ctl
    code, res = call("POST", "/api/config/proxies", {"id": "de", "ovpn": "Germany.Berlin.TCP.ovpn"})
    assert res["ok"], res
    assert app.ctl is ctl_before and set(app.ctl.instances) == {"jp", "sg", "us"}
    assert app.mgr.cfg.proxy("de") is not None
    code, res = call("POST", "/api/ovpn", {"filename": "../evil.ovpn", "content": "remote 1.1.1.1"})
    assert res["ok"] and (project / "ovpn" / "evil.ovpn").exists()
    code, res = call("POST", "/api/ovpn", {"filename": "x.txt", "content": "remote 1.1.1.1"})
    assert not res["ok"]
    code, res = call("POST", "/api/config", {"yaml": "max_connections: 1\nproxies:\n- {id: a, ovpn: Japan.Tokyo.TCP.ovpn}\n- {id: b, ovpn: Singapore.TCP.ovpn}\n"})
    assert not res["ok"] and "max_connections" in res["error"]


def test_web_starts_with_fresh_folder(tmp_path, monkeypatch):
    from hmaproxy import web as webmod

    monkeypatch.setattr(webmod.Monitor, "run_in_thread", lambda self: None)
    app = webmod.WebApp(tmp_path / "config.yaml")
    assert (tmp_path / "config.yaml").exists() and (tmp_path / ".env").exists()
    assert app.config_error and "ovpn" in app.config_error
    assert app.status()["credentials_set"] is False


# ---------------------------------------------------------------- CLI
def test_cli_validate_rejects_over_limit(write_config, capsys):
    path = write_config(max_connections=2)
    assert cli.main(["--lang", "en", "-c", str(path), "validate"]) == 2
    assert "max_connections = 2" in capsys.readouterr().err


def test_cli_talks_to_running_program(web, capsys):
    app, call, path = web
    p = str(path)
    assert cli.main(["-c", p, "create"]) == 0
    assert cli.main(["-c", p, "list"]) == 0
    out = capsys.readouterr()
    assert "jp" in out.out and "connected" in out.out and SECRET not in out.out + out.err
    assert cli.main(["-c", p, "export", "--format", "plain", "--all", "--host", "127.0.0.1"]) == 0
    base = app.mgr.cfg.proxy("jp").http_port
    assert capsys.readouterr().out.split() == [f"127.0.0.1:{base + i}" for i in range(3)]
    assert cli.main(["-c", p, "stop", "zz"]) == 5
    assert cli.main(["-c", p, "remove", "all"]) == 0


def test_cli_when_program_not_running(write_config, capsys):
    path = write_config(web={"port": free_base_port(1)})
    assert cli.main(["--lang", "en", "-c", str(path), "list"]) == 7
    assert "not running" in capsys.readouterr().err
