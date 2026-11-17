"""Rotating gateway (FR-6 extension).

One listening port that forwards each new client connection, byte for byte, to
one of the healthy per-VPN proxies, chosen round-robin or at random. Because it
works at TCP level it is protocol-agnostic: an HTTP gateway forwards to the
HTTP proxies, a SOCKS5 gateway to the SOCKS5 ports, and the client's proxy
credentials pass straight through to the upstream proxy.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import threading
from typing import Callable

from .i18n import t
from .logging_setup import get_logger

Backends = Callable[[], list[tuple[str, int]]]


class Gateway:
    def __init__(self, bind: str, port: int, backends: Backends, mode: str = "roundrobin",
                 connect_timeout: float = 10.0, name: str = "gateway"):
        self.bind = bind
        self.port = port
        self.backends = backends
        self.mode = mode
        self.connect_timeout = connect_timeout
        self.log = get_logger(name)
        self._counter = itertools.count()
        self._rng = random.SystemRandom()
        self._server: asyncio.base_events.Server | None = None

    def choose(self) -> list[tuple[str, int]]:
        """Return backends in the order to try them (first = chosen one)."""
        pool = list(self.backends())
        if not pool:
            return []
        if self.mode == "random":
            self._rng.shuffle(pool)
            return pool
        start = next(self._counter) % len(pool)
        return pool[start:] + pool[:start]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream = None
        for host, port in self.choose():
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(host, port), self.connect_timeout)
                break
            except (OSError, asyncio.TimeoutError) as exc:
                self.log.warning("upstream %s:%s unreachable: %s", host, port, exc)
        if upstream is None:
            self.log.warning(t("gateway_no_backends"))
            writer.close()
            return
        up_reader, up_writer = upstream
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.bind, self.port, reuse_address=True)

    @property
    def bound_port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    def close(self) -> None:
        if self._server is not None:
            self._server.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass
        writer.close()


def run_gateways_in_thread(gateways: list[Gateway]) -> threading.Thread:
    """Run gateways on a private event loop in a daemon thread (used by the monitor)."""

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(asyncio.gather(*(g.serve_forever() for g in gateways)))
        except Exception as exc:  # pragma: no cover - logged for operators
            get_logger("gateway").error("gateway stopped: %s", exc)

    th = threading.Thread(target=runner, name="hmaproxy-gateway", daemon=True)
    th.start()
    return th
