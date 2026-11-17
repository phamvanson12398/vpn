"""OpenVPNInstance against a fake openvpn.exe that speaks the real management protocol."""

import sys
import textwrap
import time
from pathlib import Path

import pytest

from hmaproxy.openvpn import OpenVPNInstance, OpenVPNSettings, sanitize_ovpn
from hmaproxy.providers import Credentials
from tests.conftest import SECRET

HMA_OVPN = """\
client
dev tun
proto tcp
remote 1.2.3.4 443
redirect-gateway def1
dhcp-option DNS 10.0.0.1
block-outside-dns
auth-user-pass creds.txt
up /etc/evil.sh
route 10.0.0.0 255.0.0.0
<ca>
-----BEGIN CERTIFICATE-----
route inside a cert block must stay
-----END CERTIFICATE-----
</ca>
"""


def test_sanitize_removes_routing_and_dns_hijack():
    out = sanitize_ovpn(HMA_OVPN, route_metric=9000)
    active = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert "redirect-gateway def1" not in active
    assert "block-outside-dns" not in active and "dhcp-option DNS 10.0.0.1" not in active
    assert "up /etc/evil.sh" not in active and "auth-user-pass creds.txt" not in active
    assert "route 10.0.0.0 255.0.0.0" not in active
    assert "route inside a cert block must stay" in active
    assert 'pull-filter ignore "redirect-gateway"' in active
    assert "route 0.0.0.0 0.0.0.0 vpn_gateway 9000" in active
    assert "auth-user-pass" in active and "script-security 1" in active
    assert "remote 1.2.3.4 443" in active


FAKE_OPENVPN = textwrap.dedent(r'''
    import socket, sys, time
    args = sys.argv[1:]
    i = args.index("--management")
    host, port, pwfile = args[i + 1], int(args[i + 2]), args[i + 3]
    token = open(pwfile).read().strip()
    mode = open(args[args.index("--config") + 1]).read()
    print("OpenVPN fake starting", flush=True)
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port)); srv.listen(1)
    c, _ = srv.accept(); f = c.makefile("rwb", buffering=0)
    c.sendall(b"ENTER PASSWORD:")
    if f.readline().strip().decode() != token:
        sys.exit(9)
    c.sendall(b"SUCCESS: password is correct\r\n>HOLD:Waiting for hold release:0\r\n")
    user = pw = None
    while True:
        line = f.readline()
        if not line:
            sys.exit(0)
        line = line.decode().strip()
        if line == "hold release":
            c.sendall(b">STATE:1,WAIT,,,,,,\r\n>PASSWORD:Need 'Auth' username/password\r\n")
        elif line.startswith("username"):
            user = line.split(" ", 2)[2]
        elif line.startswith("password"):
            pw = line.split(" ", 2)[2]
            if pw == '"WRONG"':
                print("AUTH: Received control message: AUTH_FAILED", flush=True)
                sys.exit(1)
            print("got", user, flush=True)
            c.sendall(b">STATE:2,CONNECTED,SUCCESS,10.8.0.6,1.2.3.4,443,,\r\n")
        elif line == "signal SIGTERM":
            c.sendall(b">STATE:3,EXITING,SIGTERM,,,,,\r\n")
            sys.exit(0)
''')


@pytest.fixture
def fake_exe(tmp_path):
    script = tmp_path / "fake_openvpn.py"
    script.write_text(FAKE_OPENVPN)
    return script


def _instance(tmp_path, fake_exe, password=SECRET):
    import subprocess

    ovpn = tmp_path / "Japan.ovpn"
    ovpn.write_text(HMA_OVPN)

    def popen(args, **kw):
        kw.pop("creationflags", None)
        return subprocess.Popen([sys.executable, str(fake_exe), *args[1:]], **kw)

    settings = OpenVPNSettings(exe=Path("openvpn.exe"), work_dir=tmp_path / "state")
    return OpenVPNInstance("jp", ovpn, "HMA-Proxy-jp", settings, lambda: Credentials("vpnuser", password),
                           popen=popen)


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_connects_via_management_and_stops_gracefully(tmp_path, fake_exe):
    inst = _instance(tmp_path, fake_exe)
    inst.start()
    assert _wait(lambda: inst.status == "connected"), (inst.status, list(inst.lines))
    assert inst.local_ip == "10.8.0.6" and inst.remote_ip == "1.2.3.4"
    assert any('got "vpnuser"' in ln for ln in inst.lines)
    # password never written to disk, never in captured output
    leftovers = " ".join(p.read_text(errors="ignore") for p in (tmp_path / "state").rglob("*") if p.is_file())
    assert SECRET not in leftovers and SECRET not in "\n".join(inst.lines)
    assert not list((tmp_path / "state").rglob("*.mgmt"))  # management token file removed
    cfg_text = (tmp_path / "state" / "run" / "jp.ovpn").read_text()
    assert "pull-filter ignore \"redirect-gateway\"" in cfg_text
    inst.stop(timeout=5)
    assert inst.status == "stopped" and inst.local_ip is None


def test_auth_failure_is_reported(tmp_path, fake_exe):
    inst = _instance(tmp_path, fake_exe, password="WRONG")
    inst.start()
    assert _wait(lambda: inst.status == "exited")
    assert "AUTH_FAILED" in inst.last_error


def test_state_line_parsing(tmp_path, fake_exe):
    inst = _instance(tmp_path, fake_exe)
    inst.handle_line(">STATE:1,CONNECTED,SUCCESS,10.1.2.3,5.6.7.8,1194,,")
    assert inst.status == "connected" and inst.local_ip == "10.1.2.3"
    inst.handle_line(">STATE:2,RECONNECTING,ping-restart,,,,,")
    assert inst.status == "connecting" and inst.local_ip is None
