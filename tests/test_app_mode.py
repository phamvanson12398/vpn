"""App mode (provider: hma-app): one proxy through the official HMA app's adapter."""

import time

import pytest

from hmaproxy import winutil
from hmaproxy.app_vpn import AppVpnInstance
from hmaproxy.config import load_config, load_credentials
from hmaproxy.service import Manager
from hmaproxy.state import StateStore
from hmaproxy.vpn_ctl import VpnController

ETH = {"name": "Ethernet", "description": "Realtek PCIe GbE", "ip": "192.168.1.20"}
HMA = {"name": "Ethernet 3", "description": "HMA VPN TAP-Windows Adapter V9", "ip": "127.0.0.1"}


def test_config_needs_no_ovpn_or_password(write_config, project):
    for f in (project / "ovpn").iterdir():
        f.unlink()
    (project / ".env").write_text("")
    cfg = load_config(write_config(provider="hma-app", proxies=[{"id": "x", "ovpn": "nope.ovpn"}]))
    assert cfg.app_mode and [p.id for p in cfg.proxies] == ["hma"]
    assert cfg.proxies[0].http_port == cfg.base_port
    assert load_credentials(cfg).user == ""
    assert cfg.adapter_patterns() == ["hma", "hidemyass", "privax"]


def test_instance_follows_the_app_adapter():
    adapters = [ETH]
    inst = AppVpnInstance("hma", patterns=lambda: ["hma"], detect=lambda: list(adapters), interval=60)
    inst.start()
    assert inst.status == "connecting" and inst.local_ip is None and "HMA" in inst.last_error
    adapters.append(HMA)
    inst.poll()
    assert inst.status == "connected" and inst.local_ip == "127.0.0.1"
    adapters.remove(HMA)  # user disconnects the HMA app
    inst.poll()
    assert inst.status == "connecting" and inst.local_ip is None
    inst.stop()
    assert inst.status == "stopped"


def test_never_falls_back_to_the_real_adapter():
    inst = AppVpnInstance("hma", patterns=lambda: ["hma"], detect=lambda: [ETH], interval=60)
    inst.start()
    assert inst.local_ip is None  # the physical card is never used as a source


@pytest.fixture
def app_manager(write_config, hub, monkeypatch):
    adapters = [ETH]
    monkeypatch.setattr(winutil, "list_adapters", lambda: list(adapters))
    cfg = load_config(write_config(provider="hma-app"))
    store = StateStore(cfg.state_dir)
    # real controller + real AppVpnInstance; no OpenVPN, no admin needed
    ctl = VpnController(cfg, store, credentials=lambda: load_credentials(cfg), hub=hub)
    return Manager(cfg, ctl=ctl, store=store), adapters


def test_create_without_openvpn_or_admin(app_manager, monkeypatch):
    mgr, adapters = app_manager
    monkeypatch.setattr("hmaproxy.vpn_ctl.is_admin", lambda: False)
    monkeypatch.setattr(mgr.ctl, "exe", None)
    assert mgr.create().ok
    row = mgr.rows()[0]
    assert row["id"] == "hma" and row["status"] == "connecting" and "HMA" in row["last_error"]
    adapters.append(HMA)
    mgr.ctl.instances["hma"].poll()
    row = mgr.rows()[0]
    assert row["status"] == "connected" and row["tunnel_ip"] == "127.0.0.1"
    assert mgr.real_ip() is None  # no false "leak" alarm: the app routes the whole PC
    mgr.ctl.shutdown()
