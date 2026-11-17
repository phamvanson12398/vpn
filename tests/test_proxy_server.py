"""End-to-end tests of the built-in HTTP / SOCKS5 proxy (tunnel IP faked as 127.0.0.1)."""

import http.server
import socket
import threading

import pytest
import requests

from hmaproxy.dns_resolve import ResolveError
from hmaproxy.proxy_server import Auth, ProxyHub, TunnelProxy
from tests.conftest import free_base_port


class StubResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, host, source_ip):
        self.calls.append((host, source_ip))
        if host == "site.test" or host == "127.0.0.1":
            return ["127.0.0.1"]
        raise ResolveError(f"cannot resolve {host}")


class Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = f"hello from {self.client_address[0]} path={self.path} proxyauth={self.headers.get('Proxy-Authorization')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def origin():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def setup():
    hub = ProxyHub()
    state = {"src": "127.0.0.1"}
    resolver = StubResolver()
    created = []

    def make(auth=None):
        base = free_base_port(2)
        p = TunnelProxy("jp", "127.0.0.1", base, base + 1, source=lambda: state["src"], resolver=resolver,
                        auth=auth, allow_local=True)
        hub.add(p)
        created.append(p)
        return p

    yield make, state, resolver
    hub.stop()


def test_http_forward_and_dns_through_tunnel(setup, origin):
    make, state, resolver = setup
    p = make()
    proxies = {"http": f"http://127.0.0.1:{p.http_port}"}
    r = requests.get(f"http://site.test:{origin}/abc", proxies=proxies, timeout=5)
    assert r.status_code == 200 and "path=/abc" in r.text and "proxyauth=None" in r.text
    assert resolver.calls[-1] == ("site.test", "127.0.0.1")  # resolved with the tunnel as source


def test_connect_tunnel(setup, origin):
    make, *_ = setup
    p = make()
    s = socket.create_connection(("127.0.0.1", p.http_port))
    s.sendall(f"CONNECT site.test:{origin} HTTP/1.1\r\nHost: site.test\r\n\r\n".encode())
    assert s.recv(100).startswith(b"HTTP/1.1 200")
    s.sendall(b"GET /x HTTP/1.0\r\n\r\n")
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    assert b"path=/x" in data


def test_socks5_with_remote_dns(setup, origin):
    make, *_ = setup
    p = make()
    proxies = {"http": f"socks5h://127.0.0.1:{p.socks_port}"}
    r = requests.get(f"http://site.test:{origin}/s", proxies=proxies, timeout=5)
    assert r.status_code == 200 and "path=/s" in r.text


def test_kill_switch_when_tunnel_down(setup, origin):
    make, state, _ = setup
    p = make()
    state["src"] = None  # VPN disconnected
    r = requests.get(f"http://site.test:{origin}/", proxies={"http": f"http://127.0.0.1:{p.http_port}"}, timeout=5)
    assert r.status_code == 503
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get(f"http://site.test:{origin}/", proxies={"http": f"socks5h://127.0.0.1:{p.socks_port}"}, timeout=5)


def test_auth_required(setup, origin):
    make, *_ = setup
    p = make(auth=Auth("me", "pw"))
    url = f"http://site.test:{origin}/a"
    assert requests.get(url, proxies={"http": f"http://127.0.0.1:{p.http_port}"}, timeout=5).status_code == 407
    r = requests.get(url, proxies={"http": f"http://me:pw@127.0.0.1:{p.http_port}"}, timeout=5)
    assert r.status_code == 200 and "proxyauth=None" in r.text  # credentials not forwarded upstream
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get(url, proxies={"http": f"socks5h://me:bad@127.0.0.1:{p.socks_port}"}, timeout=5)
    assert requests.get(url, proxies={"http": f"socks5h://me:pw@127.0.0.1:{p.socks_port}"}, timeout=5).ok


def test_unresolvable_host(setup):
    make, *_ = setup
    p = make()
    r = requests.get("http://nope.test/", proxies={"http": f"http://127.0.0.1:{p.http_port}"}, timeout=5)
    assert r.status_code == 502


def test_local_targets_blocked_by_default(origin):
    hub = ProxyHub()
    base = free_base_port(1)
    p = TunnelProxy("x", "127.0.0.1", base, None, source=lambda: "127.0.0.1", resolver=StubResolver())
    hub.add(p)
    try:
        r = requests.get(f"http://127.0.0.1:{origin}/", proxies={"http": f"http://127.0.0.1:{base}"}, timeout=5)
        assert r.status_code == 502
    finally:
        hub.stop()


def test_port_in_use_is_reported():
    from hmaproxy.errors import HmaProxyError

    hub = ProxyHub()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        p = TunnelProxy("x", "127.0.0.1", blocker.getsockname()[1], None, source=lambda: None, resolver=StubResolver())
        with pytest.raises(HmaProxyError):
            hub.add(p)
    finally:
        blocker.close()
        hub.stop()


def test_concurrent_connections(setup, origin):
    """Spec: at least 50 simultaneous connections per proxy."""
    from concurrent.futures import ThreadPoolExecutor

    make, *_ = setup
    p = make()
    proxies = {"http": f"http://127.0.0.1:{p.http_port}"}
    with ThreadPoolExecutor(60) as pool:
        codes = list(pool.map(lambda i: requests.get(f"http://site.test:{origin}/{i}", proxies=proxies,
                                                      timeout=10).status_code, range(60)))
    assert codes == [200] * 60
