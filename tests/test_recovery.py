import time

from hmaproxy.health import CheckResult
from hmaproxy.monitor import Monitor
from hmaproxy.recover import backoff_delay


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def test_backoff_grows_and_caps(manager):
    cfg = manager().cfg
    assert [backoff_delay(cfg, n) for n in range(1, 7)] == [30, 60, 120, 240, 480, 600]


def _monitor(mgr, monkeypatch, results):
    clock = Clock()
    mon = Monitor(mgr, clock=clock)
    monkeypatch.setattr(mgr, "real_ip", lambda refresh=False: "9.9.9.9")
    monkeypatch.setattr("hmaproxy.monitor.check_many",
                        lambda cfg, targets, real_ip: [results(pid) for pid, _, _ in targets])
    return mon, clock


def ok(pid):
    return CheckResult(pid, True, ip=f"1.1.1.{len(pid)}")


def test_create_starts_tunnels_and_listeners(manager, instances):
    mgr = manager()
    assert mgr.create().ok
    assert set(instances) == {"jp", "sg", "us"}
    assert all(i.status == "connected" for i in instances.values())
    assert set(mgr.ctl.hub.proxies) == {"jp", "sg", "us"}
    saved = mgr.store.get_proxy("jp")
    assert saved["spec"]["http_port"] == mgr.cfg.proxy("jp").http_port and saved["desired"] == "running"


def test_crashed_vpn_is_restarted_on_next_tick(manager, instances, monkeypatch):
    mgr = manager()
    mgr.create()
    instances["jp"].crash()
    mon, _ = _monitor(mgr, monkeypatch, ok)
    mon.tick()
    assert instances["jp"].status == "connected"
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 1


def test_user_stopped_proxy_is_left_alone(manager, instances, monkeypatch):
    mgr = manager()
    mgr.create()
    mgr.stop("jp")
    assert "jp" not in mgr.ctl.hub.proxies  # port closed while stopped
    mon, _ = _monitor(mgr, monkeypatch, ok)
    mon.tick()
    assert instances["jp"].status == "stopped"


def test_escalation_restart_then_switch_then_failed(manager, instances, monkeypatch):
    mgr = manager(proxies=[{"id": "jp", "ovpn": "Japan.Tokyo.TCP.ovpn"}],
                  recovery={"max_restarts": 2, "backoff_base_seconds": 30, "max_server_switches": 1})
    mgr.create()
    port = mgr.cfg.proxy("jp").http_port
    mon, clock = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, error="timeout"))

    def fail_until_recovery():
        for _ in range(3):
            mon.tick()
            clock.now += 60

    fail_until_recovery()
    st = mgr.store.get_proxy("jp")
    assert st["restart_attempts"] == 1 and st["health"] == "starting"
    fail_until_recovery()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 2
    fail_until_recovery()  # restarts exhausted -> other Japan server, same id/port
    st = mgr.store.get_proxy("jp")
    assert st["server_switches"] == 1 and st["ovpn"] == "Japan.Osaka.TCP.ovpn"
    assert instances["jp"].server.filename == "Japan.Osaka.TCP.ovpn"
    assert mgr.ctl.specs["jp"].http_port == port and st["spec"]["ovpn"] == "Japan.Osaka.TCP.ovpn"
    for _ in range(3):
        fail_until_recovery()
    assert mgr.store.get_proxy("jp")["health"] == "failed"

    monkeypatch.setattr("hmaproxy.monitor.check_many",
                        lambda cfg, targets, real_ip: [CheckResult(p, True, ip="5.5.5.5") for p, _, _ in targets])
    mon.tick()
    st = mgr.store.get_proxy("jp")
    assert st["health"] == "healthy" and st["restart_attempts"] == 0 and st["ip"] == "5.5.5.5"


def test_backoff_blocks_rapid_retries(manager, monkeypatch):
    mgr = manager(proxies=[{"id": "jp", "ovpn": "Japan.Tokyo.TCP.ovpn"}], health_check={"failure_threshold": 1})
    mgr.create()
    mon, clock = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, error="down"))
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 1
    clock.now += 5
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 1
    clock.now += 30
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 2


def test_ip_leak_stops_proxy(manager, instances, monkeypatch):
    mgr = manager()
    mgr.create()
    mon, _ = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, ip="9.9.9.9", leak=True, error="IP LEAK")
                      if pid == "sg" else ok(pid))
    mon.tick()
    assert instances["sg"].status == "stopped"
    st = mgr.store.get_proxy("sg")
    assert st["health"] == "failed" and st["desired"] == "stopped"


def test_duplicate_tunnel_ip_triggers_reconnect(manager, instances, monkeypatch):
    mgr = manager()
    mgr.create()
    instances["sg"].connected_at += 5  # sg connected later than jp, both on 127.0.0.1 in the fake
    instances["us"].connected_at += 10
    mon, _ = _monitor(mgr, monkeypatch, ok)
    mon.tick()
    assert instances["jp"].calls == ["start"]
    assert instances["sg"].calls[-2:] == ["stop", "start"]


def test_restore_after_program_restart(manager, make_ctl, instances):
    mgr = manager()
    mgr.create()
    mgr.stop("us")
    mgr.ctl.shutdown()
    mgr.ctl.hub.stop()
    instances.clear()
    from hmaproxy.proxy_server import ProxyHub

    ctl2 = make_ctl(mgr.cfg)
    ctl2.hub = ProxyHub()
    try:
        started = ctl2.restore()
        assert started == ["jp", "sg"]
        assert instances["us"].status == "stopped"
    finally:
        ctl2.shutdown()
        ctl2.hub.stop()


def test_grace_period_delays_recovery(manager, monkeypatch):
    mgr = manager(health_check={"failure_threshold": 1, "startup_grace_seconds": 90})
    mgr.create()
    mon, clock = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, error="connecting"))
    clock.now = mgr.store.get_proxy("jp")["started_at"] + 10
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 0
    assert mgr.store.get_proxy("jp")["health"] == "starting"
