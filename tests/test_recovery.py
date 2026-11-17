from hmaproxy.health import CheckResult
from hmaproxy.monitor import Monitor
from hmaproxy.recover import backoff_delay


class Clock:
    def __init__(self):
        self.now = __import__("time").time()

    def __call__(self):
        return self.now


def test_backoff_grows_and_caps(manager):
    cfg = manager().cfg
    assert [backoff_delay(cfg, n) for n in range(1, 7)] == [30, 60, 120, 240, 480, 600]


def _monitor(mgr, monkeypatch, results):
    """Monitor whose health checks return the given {id: CheckResult-ish} map."""
    clock = Clock()
    mon = Monitor(mgr, clock=clock)
    monkeypatch.setattr(mgr, "real_ip", lambda refresh=False: "9.9.9.9")

    def fake_check_many(cfg, targets, real_ip):
        return [results(pid) for pid, _, _ in targets]

    monkeypatch.setattr("hmaproxy.monitor.check_many", fake_check_many)
    return mon, clock


def test_stopped_container_is_restarted_on_next_tick(manager, fake_docker, monkeypatch):
    mgr = manager()
    mgr.create()
    fake_docker.containers.items["hma-proxy-jp"].status = "exited"  # killed behind our back
    mon, _ = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, True, ip=f"1.1.1.{len(pid)}"))
    mon.tick()
    assert fake_docker.containers.items["hma-proxy-jp"].status == "running"
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 1


def test_user_stopped_proxy_is_left_alone(manager, fake_docker, monkeypatch):
    mgr = manager()
    mgr.create()
    mgr.stop("jp")
    mon, _ = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, True, ip="1.1.1.1"))
    mon.tick()
    assert fake_docker.containers.items["hma-proxy-jp"].status == "exited"


def test_escalation_restart_then_switch_then_failed(manager, fake_docker, monkeypatch):
    mgr = manager(proxies=[{"id": "jp", "ovpn": "Japan.Tokyo.TCP.ovpn"}],
                  recovery={"max_restarts": 2, "backoff_base_seconds": 30, "max_server_switches": 1})
    mgr.create()
    mon, clock = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, error="timeout"))

    def fail_until_recovery():
        for _ in range(3):  # failure_threshold = 3
            mon.tick()
            clock.now += 60

    fail_until_recovery()
    st = mgr.store.get_proxy("jp")
    assert st["restart_attempts"] == 1 and st["health"] == "starting"
    assert "restart" in fake_docker.containers.items["hma-proxy-jp"].calls

    fail_until_recovery()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 2

    fail_until_recovery()  # restarts exhausted -> switch to another Japan server
    st = mgr.store.get_proxy("jp")
    assert st["server_switches"] == 1 and st["ovpn"] == "Japan.Osaka.TCP.ovpn"
    assert fake_docker.containers.items["hma-proxy-jp"].labels["hmaproxy.ovpn"] == "Japan.Osaka.TCP.ovpn"
    assert fake_docker.containers.items["hma-proxy-jp"].kwargs["ports"] == {"8888/tcp": ("127.0.0.1", 8881)}

    for _ in range(3):
        fail_until_recovery()
    assert mgr.store.get_proxy("jp")["health"] == "failed"

    # a later success heals everything
    monkeypatch.setattr("hmaproxy.monitor.check_many",
                        lambda cfg, targets, real_ip: [CheckResult(p, True, ip="5.5.5.5") for p, _, _ in targets])
    mon.tick()
    st = mgr.store.get_proxy("jp")
    assert st["health"] == "healthy" and st["restart_attempts"] == 0 and st["ip"] == "5.5.5.5"


def test_backoff_blocks_rapid_retries(manager, fake_docker, monkeypatch):
    mgr = manager(proxies=[{"id": "jp", "ovpn": "Japan.Tokyo.TCP.ovpn"}], health_check={"failure_threshold": 1})
    mgr.create()
    mon, clock = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, error="down"))
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 1
    clock.now += 5  # within the 30s backoff
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 1
    clock.now += 30
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 2


def test_ip_leak_stops_proxy(manager, fake_docker, monkeypatch):
    mgr = manager()
    mgr.create()
    mon, _ = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, ip="9.9.9.9", leak=True, error="IP LEAK")
                      if pid == "sg" else CheckResult(pid, True, ip="1.2.3.4"))
    mon.tick()
    assert fake_docker.containers.items["hma-proxy-sg"].status == "exited"
    st = mgr.store.get_proxy("sg")
    assert st["health"] == "failed" and st["desired"] == "stopped"


def test_grace_period_delays_recovery(manager, fake_docker, monkeypatch):
    mgr = manager(health_check={"failure_threshold": 1, "startup_grace_seconds": 90})
    mgr.create()
    mon, clock = _monitor(mgr, monkeypatch, lambda pid: CheckResult(pid, False, error="connecting"))
    clock.now = mgr.store.get_proxy("jp")["started_at"] + 10
    mon.tick()
    assert mgr.store.get_proxy("jp")["restart_attempts"] == 0
    assert mgr.store.get_proxy("jp")["health"] == "starting"
