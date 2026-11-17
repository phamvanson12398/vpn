import stat

from tests.conftest import SECRET


def test_create_builds_safe_containers(manager, fake_docker):
    mgr = manager(socks5={"enabled": True}, proxy_auth={"enabled": True, "user": "me", "password": "proxypw"})
    report = mgr.create()
    assert report.ok, report.results
    items = fake_docker.containers.items
    assert set(items) == {"hma-proxy-jp", "hma-proxy-jp-socks", "hma-proxy-sg", "hma-proxy-sg-socks",
                          "hma-proxy-us", "hma-proxy-us-socks"}
    vpn = items["hma-proxy-jp"]
    env = vpn.kwargs["environment"]
    # HMA password never in env / labels / command
    assert SECRET not in repr(vpn.kwargs) and SECRET not in repr(vpn.labels)
    assert env["VPN_SERVICE_PROVIDER"] == "custom" and env["HTTPPROXY"] == "on"
    assert env["HTTPPROXY_USER"] == "me" and env["FIREWALL_INPUT_PORTS"] == "1080"
    assert vpn.kwargs["cap_add"] == ["NET_ADMIN"]
    assert vpn.kwargs["ports"] == {"8888/tcp": ("127.0.0.1", 8881), "1080/tcp": ("127.0.0.1", 1081)}
    assert vpn.status == "running"
    socks = items["hma-proxy-jp-socks"]
    assert socks.kwargs["network_mode"] == "container:hma-proxy-jp"
    assert "ports" not in socks.kwargs
    # secrets written with mode 600
    pw_file = mgr.cfg.state_dir / "secrets" / "hma_password"
    assert pw_file.read_text() == SECRET
    assert stat.S_IMODE(pw_file.stat().st_mode) == 0o600
    mounts = {v["bind"]: k for k, v in vpn.kwargs["volumes"].items()}
    assert mounts["/run/secrets/openvpn_password"].endswith("hma_password")
    assert fake_docker.images.pulled == ["qmcgaw/gluetun:v3.40.0", "gogost/gost:3.0.0"]


def test_create_is_idempotent_and_respects_limit(manager, fake_docker):
    mgr = manager()
    assert mgr.create().ok
    again = mgr.create()
    assert again.ok and all("already exists" in r.message for r in again.results)


def test_lifecycle_and_list(manager, fake_docker):
    mgr = manager()
    mgr.create()
    rows = {r["id"]: r for r in mgr.rows()}
    assert rows["jp"]["status"] == "running" and rows["jp"]["http_port"] == 8881
    assert rows["jp"]["uptime"] is not None
    assert all(r.ok for r in mgr.stop("jp"))
    assert fake_docker.containers.items["hma-proxy-jp"].status == "exited"
    assert mgr.store.get_proxy("jp")["desired"] == "stopped"
    assert all(r.ok for r in mgr.start("all"))
    assert mgr.store.get_proxy("jp")["desired"] == "running"
    assert all(r.ok for r in mgr.remove("sg"))
    assert "hma-proxy-sg" not in fake_docker.containers.items
    assert {r["id"]: r["created"] for r in mgr.rows()}["sg"] is False


def test_unknown_id(manager):
    import pytest

    from hmaproxy.errors import NotFoundError

    mgr = manager()
    with pytest.raises(NotFoundError):
        mgr.start("nope")


def test_logs_are_redacted(manager):
    mgr = manager()
    mgr.create()
    text = mgr.logs("jp")
    assert SECRET not in text and "***" in text


def test_uptime_parsing():
    from hmaproxy.docker_ctl import uptime_seconds

    assert uptime_seconds("0001-01-01T00:00:00Z") is None
    assert abs(uptime_seconds("2026-01-01T00:00:00.5Z", now=1767225610.5) - 10.0) < 1e-6
