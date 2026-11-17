"""Built-in HTTP and SOCKS5 proxy servers, one pair per VPN tunnel.

Every outgoing connection is bound to the tunnel's local IP (``local_addr``),
so Windows routes it through that VPN adapter. If the tunnel is down there is
no tunnel IP: the proxy answers 503 / "network unreachable" and **never** falls
back to the PC's normal connection (application-level kill switch).
Host names are resolved through the same tunnel (see dns_resolve.py).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Callable

from .dns_resolve import ResolveError, TunnelResolver
from .errors import HmaProxyError
from .gateway import _pipe
from .i18n import t
from .logging_setup import get_logger

HANDSHAKE_TIMEOUT = 30


class Unavailable(Exception):
    """The VPN tunnel behind this proxy is not connected."""


@dataclass
class Auth:
    user: str
    password: str

    def check(self, user: str, password: str) -> bool:
        return hmac.compare_digest(user.encode(), self.user.encode()) and \
            hmac.compare_digest(password.encode(), self.password.encode())


class TunnelProxy:
    def __init__(self, proxy_id: str, bind: str, http_port: int, socks_port: int | None,
                 source: Callable[[], str | None], resolver: TunnelResolver, auth: Auth | None = None,
                 connect_timeout: float = 15.0, max_connections: int = 512, allow_local: bool = False):
        self.id = proxy_id
        self.bind = bind
        self.http_port = http_port
        self.socks_port = socks_port
        self.source = source
        self.resolver = resolver
        self.auth = auth
        self.connect_timeout = connect_timeout
        self.max_connections = max_connections
        self.allow_local = allow_local
        self.active = 0
        self.total = 0
        self.log = get_logger(proxy_id)
        self._servers: list[asyncio.base_events.Server] = []
        self._sem: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------ server
    async def start(self) -> None:
        self._sem = asyncio.Semaphore(self.max_connections)
        try:
            self._servers.append(await asyncio.start_server(self._wrap(self._http), self.bind, self.http_port))
            if self.socks_port:
                self._servers.append(await asyncio.start_server(self._wrap(self._socks), self.bind, self.socks_port))
        except OSError as exc:
            await self.close()
            port = self.socks_port if self._servers else self.http_port
            raise HmaProxyError(t("port_in_use", port=port, error=exc)) from exc

    def bound_ports(self) -> list[int]:
        return [s.sockets[0].getsockname()[1] for s in self._servers]

    async def close(self) -> None:
        for s in self._servers:
            s.close()
        for s in self._servers:
            try:
                await s.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._servers = []

    def _wrap(self, handler):
        async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            assert self._sem is not None
            async with self._sem:
                self.active += 1
                self.total += 1
                try:
                    await handler(reader, writer)
                except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("proxy handler error: %s", exc)
                finally:
                    self.active -= 1
                    if not writer.is_closing():
                        writer.close()
        return run

    # ------------------------------------------------------------------ upstream
    async def open_upstream(self, host: str, port: int):
        src = self.source()
        if not src:
            raise Unavailable()
        try:
            addrs = await asyncio.to_thread(self.resolver.resolve, host, src)
        except ResolveError:
            raise
        last: Exception | None = None
        for addr in addrs:
            if not self.allow_local and _forbidden(addr):
                last = ResolveError(f"{addr} is a local address")
                continue
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(addr, port, local_addr=(src, 0)), self.connect_timeout)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
        raise last or ResolveError(host)

    # ------------------------------------------------------------------ HTTP
    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_TIMEOUT)
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return await _respond(writer, 400, "Bad Request")
        headers = [ln for ln in lines[1:] if ln]
        if self.auth and not self._http_auth_ok(headers):
            return await _respond(writer, 407, "Proxy Authentication Required",
                                  extra='Proxy-Authenticate: Basic realm="hmaproxy"\r\n')

        if method.upper() == "CONNECT":
            host, port = _split_host_port(target, 443)
        else:
            if not target.lower().startswith("http://"):
                return await _respond(writer, 400, "Bad Request")
            rest = target[7:]
            hostport, _, path = rest.partition("/")
            host, port = _split_host_port(hostport, 80)
            path = "/" + path

        try:
            up_reader, up_writer = await self.open_upstream(host, port)
        except Unavailable:
            return await _respond(writer, 503, "Service Unavailable", body="VPN tunnel is not connected\n")
        except ResolveError as exc:
            return await _respond(writer, 502, "Bad Gateway", body=f"{exc}\n")
        except asyncio.TimeoutError:
            return await _respond(writer, 504, "Gateway Timeout")
        except OSError as exc:
            return await _respond(writer, 502, "Bad Gateway", body=f"{exc}\n")

        if method.upper() == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            drop = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}
            kept = [h for h in headers if h.split(":", 1)[0].strip().lower() not in drop]
            if not any(h.lower().startswith("host:") for h in kept):
                kept.insert(0, f"Host: {hostport}")
            request = f"{method} {path} {version}\r\n" + "".join(h + "\r\n" for h in kept) + "Connection: close\r\n\r\n"
            up_writer.write(request.encode("latin-1"))
            await up_writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True)

    def _http_auth_ok(self, headers: list[str]) -> bool:
        assert self.auth is not None
        for h in headers:
            name, _, value = h.partition(":")
            if name.strip().lower() == "proxy-authorization":
                scheme, _, cred = value.strip().partition(" ")
                if scheme.lower() != "basic":
                    return False
                try:
                    user, _, pw = base64.b64decode(cred.strip()).decode("utf-8").partition(":")
                except Exception:  # noqa: BLE001
                    return False
                return self.auth.check(user, pw)
        return False

    # ------------------------------------------------------------------ SOCKS5
    async def _socks(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def read(n: int) -> bytes:
            return await asyncio.wait_for(reader.readexactly(n), HANDSHAKE_TIMEOUT)

        ver, nmethods = await read(2)
        if ver != 5:
            return
        methods = await read(nmethods)
        wanted = 0x02 if self.auth else 0x00
        if wanted not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(bytes([5, wanted]))
        await writer.drain()
        if self.auth:
            _, ulen = await read(2)
            user = (await read(ulen)).decode("utf-8", "replace")
            (plen,) = await read(1)
            pw = (await read(plen)).decode("utf-8", "replace")
            ok = self.auth.check(user, pw)
            writer.write(b"\x01\x00" if ok else b"\x01\x01")
            await writer.drain()
            if not ok:
                return

        _, cmd, _, atyp = await read(4)
        if atyp == 1:
            host = socket.inet_ntoa(await read(4))
        elif atyp == 3:
            (ln,) = await read(1)
            host = (await read(ln)).decode("idna")
        elif atyp == 4:
            await read(16)
            await read(2)
            return await _socks_reply(writer, 0x08)
        else:
            return await _socks_reply(writer, 0x08)
        (port,) = struct.unpack("!H", await read(2))
        if cmd != 1:
            return await _socks_reply(writer, 0x07)

        try:
            up_reader, up_writer = await self.open_upstream(host, port)
        except Unavailable:
            return await _socks_reply(writer, 0x03)
        except ResolveError:
            return await _socks_reply(writer, 0x04)
        except ConnectionRefusedError:
            return await _socks_reply(writer, 0x05)
        except (OSError, asyncio.TimeoutError):
            return await _socks_reply(writer, 0x04)
        bound = up_writer.get_extra_info("sockname") or ("0.0.0.0", 0)
        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton(bound[0]) + struct.pack("!H", bound[1]))
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True)


def _forbidden(addr: str) -> bool:
    """Don't let the proxy be used to reach this PC or the LAN (DNS-rebinding style abuse)."""
    ip = ipaddress.ip_address(addr)
    return ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local


def _split_host_port(value: str, default: int) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default
    host, sep, port = value.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return value, default


async def _respond(writer: asyncio.StreamWriter, code: int, reason: str, body: str = "", extra: str = "") -> None:
    data = body.encode()
    writer.write(f"HTTP/1.1 {code} {reason}\r\n{extra}Content-Type: text/plain\r\nContent-Length: {len(data)}\r\n"
                 f"Connection: close\r\n\r\n".encode() + data)
    await writer.drain()


async def _socks_reply(writer: asyncio.StreamWriter, code: int) -> None:
    writer.write(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
    await writer.drain()


class ProxyHub:
    """Runs all TunnelProxy servers (and gateways) on one asyncio loop in a background thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.proxies: dict[str, TunnelProxy] = {}
        self._thread = threading.Thread(target=self._run, daemon=True, name="hmaproxy-proxies")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float = 15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def add(self, proxy: TunnelProxy) -> None:
        self.remove(proxy.id)
        self.call(proxy.start())
        self.proxies[proxy.id] = proxy

    def remove(self, proxy_id: str) -> None:
        old = self.proxies.pop(proxy_id, None)
        if old is not None:
            self.call(old.close())

    def has(self, proxy_id: str) -> bool:
        return proxy_id in self.proxies

    def stop(self) -> None:
        for pid in list(self.proxies):
            self.remove(pid)
        self.loop.call_soon_threadsafe(self.loop.stop)
