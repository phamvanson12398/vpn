"""One OpenVPN connection = one ``openvpn.exe`` process driven over its management interface.

Split-tunnel design (the key to running N VPNs at once on one Windows PC):

* The HMA .ovpn is copied and *sanitised*: ``redirect-gateway``, DNS changes,
  ``block-outside-dns`` and pushed routes are removed/ignored, so the tunnel
  never becomes the PC's default route and normal traffic is untouched.
* A default route is added **on the tunnel interface only**, with a very high
  metric (9000). Ordinary traffic still uses the real network card; a socket
  explicitly bound to the tunnel's IP is routed through the tunnel (Windows'
  strong host model), which is exactly what the built-in proxy does.
* The HMA password is never written to disk: OpenVPN asks for it over the
  management interface (``--management-query-passwords``) and we answer from memory.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .i18n import t
from .logging_setup import get_logger, redact, register_secret
from .providers import Credentials
from .winutil import CREATE_NO_WINDOW, KillOnCloseJob

# Directives removed from the provider's .ovpn: anything that would hijack the
# PC's routing/DNS, run scripts, read credentials from files, or set up management.
STRIP_DIRECTIVES = {
    "redirect-gateway", "redirect-private", "route", "route-ipv6", "route-gateway", "route-metric",
    "route-delay", "route-method", "route-nopull", "dhcp-option", "block-outside-dns", "register-dns",
    "dns", "pull-filter", "auth-user-pass", "auth-retry", "up", "down", "route-up", "route-pre-down",
    "ipchange", "learn-address", "tls-verify", "client-connect", "client-disconnect", "script-security",
    "management", "management-hold", "management-query-passwords", "management-client",
    "management-signal", "management-forget-disconnect", "management-up-down", "dev-node",
    "windows-driver", "log", "log-append", "daemon", "service", "ip-win32", "tap-sleep", "plugin",
    "askpass", "auth-nocache", "status", "writepid", "cd", "chroot", "config", "service", "persist-tun",
    "block-ipv6",
}
# Options removed in OpenVPN 2.6 that make it refuse to start (found in older provider files).
REMOVED_IN_26 = {"keysize", "ncp-disable", "prng", "no-iv", "no-replay", "tls-remote", "compat-names",
                 "no-name-remapping", "data-ciphers-fallback", "data-ciphers", "ncp-ciphers"}
INLINE_BLOCK_RE = re.compile(r"^<(/?)([a-z0-9-]+)>", re.IGNORECASE)


def sanitize_ovpn(text: str, route_metric: int, extra_lines: list[str] | None = None) -> str:
    out: list[str] = []
    in_block: str | None = None
    legacy_cipher: str | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        m = INLINE_BLOCK_RE.match(line)
        if m:
            closing, name = m.groups()
            in_block = None if closing else name.lower()
            out.append(raw)
            continue
        if in_block:
            out.append(raw)
            continue
        if not line or line.startswith(("#", ";")):
            out.append(raw)
            continue
        parts = line.split()
        directive = parts[0].lower().lstrip("-")
        if directive in STRIP_DIRECTIVES or directive in REMOVED_IN_26:
            out.append(f"# [hmaproxy removed] {line}")
            continue
        if directive == "ns-cert-type" and len(parts) > 1:
            # removed in OpenVPN 2.6; modern equivalent
            out.append(f"# [hmaproxy replaced] {line}")
            out.append(f"remote-cert-tls {parts[1]}")
            continue
        if directive == "cipher" and len(parts) > 1:
            legacy_cipher = parts[1]
        out.append(raw)
    out += [
        "",
        "# ---- added by HMA Proxy Manager: split tunnel, the PC's routing/DNS stay untouched ----",
        'pull-filter ignore "redirect-gateway"',
        'pull-filter ignore "redirect-private"',
        'pull-filter ignore "dhcp-option"',
        'pull-filter ignore "dns "',
        'pull-filter ignore "block-outside-dns"',
        'pull-filter ignore "register-dns"',
        'pull-filter ignore "route "',
        'pull-filter ignore "route-ipv6"',
        'pull-filter ignore "ifconfig-ipv6"',
        'pull-filter ignore "block-ipv6"',
        f"route 0.0.0.0 0.0.0.0 vpn_gateway {int(route_metric)}",
        "auth-user-pass",
        "auth-retry none",
        "script-security 1",
        "persist-key",
    ]
    if legacy_cipher and "GCM" not in legacy_cipher.upper() and "CHACHA" not in legacy_cipher.upper():
        # Older provider files (e.g. ExpressVPN) use AES-256-CBC; OpenVPN 2.6 only negotiates it if listed.
        out += [f"data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:{legacy_cipher}",
                f"data-ciphers-fallback {legacy_cipher}"]
    out += list(extra_lines or [])
    return "\n".join(out) + "\n"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class OpenVPNSettings:
    exe: Path | None
    driver: str = "tap-windows6"
    route_metric: int = 9000
    verbosity: int = 3
    extra_lines: list[str] = field(default_factory=list)
    work_dir: Path = Path(".")


class OpenVPNInstance:
    """Lifecycle of one openvpn.exe. Thread-safe; all methods return quickly."""

    def __init__(self, proxy_id: str, ovpn_path: Path, adapter: str, settings: OpenVPNSettings,
                 credentials: Callable[[], Credentials], job: KillOnCloseJob | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.id = proxy_id
        self.ovpn_path = ovpn_path
        self.adapter = adapter
        self.settings = settings
        self.credentials = credentials
        self.job = job
        self.popen = popen
        self.log = get_logger(proxy_id)

        self.status = "stopped"        # stopped | connecting | connected | exited
        self.state_name = ""           # OpenVPN state: CONNECTING, WAIT, AUTH, GET_CONFIG, ASSIGN_IP, CONNECTED, RECONNECTING...
        self.local_ip: str | None = None
        self.remote_ip: str | None = None
        self.started_at: float | None = None
        self.connected_at: float | None = None
        self.last_error: str | None = None
        self.exit_code: int | None = None
        self.lines: deque[str] = deque(maxlen=2000)

        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self._stopping = False
        self._generation = 0

    # ------------------------------------------------------------------ files
    @property
    def run_dir(self) -> Path:
        return self.settings.work_dir / "run"

    def _write_config(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        text = self.ovpn_path.read_text(encoding="utf-8", errors="replace")
        path = self.run_dir / f"{self.id}.ovpn"
        path.write_text(sanitize_ovpn(text, self.settings.route_metric, self.settings.extra_lines), encoding="utf-8")
        return path

    def build_args(self, config_path: Path, mgmt_port: int, mgmt_pw_file: Path) -> list[str]:
        exe = str(self.settings.exe or "openvpn")
        return [
            exe, "--config", str(config_path),
            "--dev-node", self.adapter,
            "--windows-driver", self.settings.driver,
            "--management", "127.0.0.1", str(mgmt_port), str(mgmt_pw_file),
            "--management-hold", "--management-query-passwords",
            "--verb", str(self.settings.verbosity),
        ]

    # ------------------------------------------------------------------ lifecycle
    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._generation += 1
            gen = self._generation
            self._stopping = False
            self.status = "connecting"
            self.state_name = "STARTING"
            self.local_ip = self.remote_ip = None
            self.connected_at = None
            self.exit_code = None
            self.last_error = None
            self.started_at = time.time()
            config_path = self._write_config()
            token = secrets.token_hex(16)
            pw_file = self.run_dir / f"{self.id}.mgmt"
            pw_file.write_text(token + "\n", encoding="utf-8")
            port = free_port()
            args = self.build_args(config_path, port, pw_file)
            self._add_line(f"$ openvpn {' '.join(args[1:])}")
            try:
                self._proc = self.popen(args, cwd=str(self.ovpn_path.parent), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        creationflags=CREATE_NO_WINDOW)
            except OSError as exc:
                self.status = "exited"
                self.last_error = str(exc)
                raise
            if self.job:
                self.job.add(self._proc)
            proc = self._proc
        threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True, name=f"ovpn-out-{self.id}").start()
        threading.Thread(target=self._manage, args=(gen, port, token, pw_file), daemon=True,
                         name=f"ovpn-mgmt-{self.id}").start()

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stopping = True
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._mark_stopped()
                return
            # Graceful: lets OpenVPN remove its routes and release the adapter.
            self._send("signal SIGTERM")
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        with self._lock:
            self._mark_stopped()

    def restart(self) -> None:
        self.stop()
        self.start()

    def _mark_stopped(self) -> None:
        self.status = "stopped"
        self.state_name = "STOPPED"
        self.local_ip = None
        self.connected_at = None
        self._close_sock()

    # ------------------------------------------------------------------ io threads
    def _add_line(self, line: str) -> None:
        self.lines.append(redact(line.rstrip()))

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            self._add_line(line)
            if "AUTH_FAILED" in line:
                self.last_error = t("vpn_auth_failed")
        code = proc.wait()
        with self._lock:
            if proc is not self._proc:
                return
            self.exit_code = code
            self.local_ip = None
            self.connected_at = None
            self._close_sock()
            if self._stopping:
                self.status = "stopped"
            else:
                self.status = "exited"
                self.state_name = "EXITED"
                if not self.last_error:
                    tail = [ln for ln in self.lines if ln and not ln.startswith("$")][-3:]
                    self.last_error = t("vpn_exited", code=code) + (": " + " | ".join(tail) if tail else "")
                self.log.warning(self.last_error)

    def _manage(self, gen: int, port: int, token: str, pw_file: Path) -> None:
        sock = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if gen != self._generation or not self.alive:
                return
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                time.sleep(0.3)
        if sock is None:
            if self.alive:
                self.last_error = "management interface did not open"
            return
        try:
            os.remove(pw_file)
        except OSError:
            pass
        with self._lock:
            if gen != self._generation:
                sock.close()
                return
            self._sock = sock
        sock.settimeout(None)
        buf = b""
        try:
            # The password prompt has no trailing newline.
            while b"ENTER PASSWORD:" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
            buf = buf.split(b"ENTER PASSWORD:", 1)[1]
            self._send(token)
            self._send("state on")
            self._send("hold release")
            while True:
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.handle_line(line.decode("utf-8", "replace").rstrip("\r"))
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass

    def _send(self, cmd: str) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(cmd.encode("utf-8") + b"\n")
        except OSError:
            pass

    def _close_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------------------ protocol
    def handle_line(self, line: str) -> None:
        """React to one line from the management interface."""
        if line.startswith(">PASSWORD:Need 'Auth'"):
            creds = self.credentials()
            register_secret(creds.password)
            self._send(f'username "Auth" {_quote(creds.user)}')
            self._send(f'password "Auth" {_quote(creds.password)}')
        elif line.startswith(">PASSWORD:Verification Failed"):
            self.last_error = t("vpn_auth_failed")
            self.log.error(self.last_error)
        elif line.startswith(">HOLD:"):
            self._send("hold release")
        elif line.startswith(">STATE:"):
            self._on_state(line[len(">STATE:"):])
        elif line.startswith(">FATAL:"):
            self.last_error = line[len(">FATAL:"):]
            self.log.error(self.last_error)
        elif line.startswith(">LOG:"):
            self._add_line(line[5:])

    def _on_state(self, payload: str) -> None:
        # time,STATE,description,local_ip,remote_ip,remote_port,local_addr,local_port,local_ipv6
        parts = payload.split(",")
        name = parts[1] if len(parts) > 1 else ""
        with self._lock:
            self.state_name = name
            if name == "CONNECTED":
                desc = parts[2] if len(parts) > 2 else ""
                if desc.startswith("SUCCESS") or desc == "":
                    self.local_ip = parts[3] if len(parts) > 3 and parts[3] else None
                    self.remote_ip = parts[4] if len(parts) > 4 and parts[4] else None
                    self.status = "connected" if self.local_ip else "connecting"
                    self.connected_at = time.time()
                    self.last_error = None
                    self.log.info(t("vpn_connected", ip=self.local_ip, server=self.remote_ip))
                else:
                    self.status = "connecting"
                    self.last_error = desc
            elif name in ("RECONNECTING", "EXITING", "WAIT", "AUTH", "GET_CONFIG", "ASSIGN_IP",
                          "ADD_ROUTES", "CONNECTING", "RESOLVE", "TCP_CONNECT", "AUTH_PENDING"):
                if name == "RECONNECTING":
                    self.log.warning(t("vpn_reconnecting", reason=parts[2] if len(parts) > 2 else ""))
                self.status = "connecting"
                self.local_ip = None
                self.connected_at = None
