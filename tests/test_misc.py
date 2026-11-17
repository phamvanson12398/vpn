import asyncio
import json
import logging

from hmaproxy.export import export_proxies
from hmaproxy.gateway import Gateway
from hmaproxy.health import CheckResult, check_proxy, mark_duplicates
from hmaproxy.logging_setup import get_logger, register_secret, setup_logging
from tests.conftest import SECRET

ROWS = [
    {"id": "jp", "country": "Japan", "http_port": 8881, "socks_port": 1081, "ip": "1.1.1.1", "health": "healthy"},
    {"id": "sg", "country": "Singapore", "http_port": 8882, "socks_port": 1082, "ip": None, "health": "unhealthy"},
]


def test_export_formats(manager):
    cfg = manager(socks5={"enabled": True}).cfg
    assert export_proxies(cfg, ROWS, "plain", host="127.0.0.1") == "127.0.0.1:8881\n127.0.0.1:8882"
    assert export_proxies(cfg, ROWS, "http", host="h", only_healthy=True) == "http://h:8881"
    assert export_proxies(cfg, ROWS, "socks5", host="h").splitlines()[1] == "socks5://h:1082"
    data = json.loads(export_proxies(cfg, ROWS, "json", host="h"))
    assert data[0]["http"] == "http://h:8881" and data[0]["exit_ip"] == "1.1.1.1"


def test_export_with_auth(manager):
    cfg = manager(proxy_auth={"enabled": True, "user": "me", "password": "p@ss"}).cfg
    assert export_proxies(cfg, ROWS[:1], "http", host="h") == "http://me:p%40ss@h:8881"
    assert export_proxies(cfg, ROWS[:1], "plain", host="h") == "h:8881:me:p@ss"
    assert export_proxies(cfg, ROWS[:1], "http", host="h", include_auth=False) == "http://h:8881"


def test_logging_redacts_secrets(tmp_path, capsys):
    log_file = tmp_path / "x.log"
    setup_logging(log_file, console_level="INFO")
    register_secret(SECRET)
    get_logger("jp").error("connect failed for user with password %s", SECRET)
    try:
        raise RuntimeError(f"boom {SECRET}")
    except RuntimeError:
        get_logger("jp").exception("oops")
    for h in logging.getLogger("hmaproxy").handlers:
        h.flush()
    content = log_file.read_text()
    assert SECRET not in content and SECRET not in capsys.readouterr().err
    assert "[jp]" in content and "***" in content


def test_check_proxy_detects_leak(manager, monkeypatch):
    cfg = manager().cfg
    monkeypatch.setattr("hmaproxy.health.fetch_ip", lambda urls, proxy, timeout: "9.9.9.9")
    r = check_proxy(cfg, "jp", 8881, None, real_ip="9.9.9.9")
    assert not r.ok and r.leak
    r = check_proxy(cfg, "jp", 8881, None, real_ip="1.2.3.4")
    assert r.ok and r.ip == "9.9.9.9"


def test_check_proxy_socks_failure(manager, monkeypatch):
    cfg = manager(socks5={"enabled": True}).cfg

    def fake(urls, proxy, timeout):
        if proxy.startswith("socks5h://"):
            raise ConnectionError("refused")
        return "5.5.5.5"

    monkeypatch.setattr("hmaproxy.health.fetch_ip", fake)
    r = check_proxy(cfg, "jp", 8881, 1081, real_ip="1.2.3.4")
    assert r.ok and r.socks_ok is False and "refused" in r.socks_error


def test_duplicates_flagged():
    rs = [CheckResult("a", True, ip="1.1.1.1"), CheckResult("b", True, ip="1.1.1.1"), CheckResult("c", True, ip="2.2.2.2")]
    mark_duplicates(rs)
    assert rs[0].duplicate_of == ["b"] and rs[2].duplicate_of == []


def test_gateway_round_robin():
    async def scenario():
        async def backend(tag):
            async def handle(r, w):
                await r.read(5)
                w.write(tag)
                await w.drain()
                w.close()
            srv = await asyncio.start_server(handle, "127.0.0.1", 0)
            return srv, srv.sockets[0].getsockname()[1]

        s1, p1 = await backend(b"A")
        s2, p2 = await backend(b"B")
        gw = Gateway("127.0.0.1", 0, lambda: [("127.0.0.1", p1), ("127.0.0.1", 1), ("127.0.0.1", p2)])
        await gw.start()
        task = asyncio.create_task(gw.serve_forever())
        seen = []
        for _ in range(3):
            r, w = await asyncio.open_connection("127.0.0.1", gw.bound_port)
            w.write(b"hello")
            await w.drain()
            seen.append(await r.read())
            w.close()
        gw.close()
        task.cancel()
        s1.close()
        s2.close()
        return seen

    # 2nd pick hits the dead backend (port 1) and falls through to the next one
    assert asyncio.run(scenario()) == [b"A", b"B", b"B"]
