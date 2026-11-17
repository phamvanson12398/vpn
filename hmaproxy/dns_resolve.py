"""DNS resolution *through a specific VPN tunnel*.

Queries are sent from the tunnel's IP address, so they leave through the VPN
and the PC's own DNS server never sees which sites a proxy visits.
"""

from __future__ import annotations

import ipaddress
import threading
import time

import dns.exception
import dns.message
import dns.query
import dns.rdatatype


class ResolveError(Exception):
    pass


class TunnelResolver:
    def __init__(self, servers: list[str], timeout: float = 5.0, cache_seconds: int = 300):
        self.servers = servers
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def resolve(self, host: str, source_ip: str) -> list[str]:
        """IPv4 addresses for ``host`` (IPv4 only: the tunnel has no IPv6, so no v6 leak)."""
        host = host.strip().rstrip(".").lower()
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        else:
            if ip.version != 4:
                raise ResolveError("IPv6 destinations are not supported through the tunnel")
            return [str(ip)]
        if host == "localhost" or not host:
            raise ResolveError(f"refusing to resolve {host!r}")

        key = (source_ip, host)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]

        errors = []
        for server in self.servers:
            try:
                q = dns.message.make_query(host, dns.rdatatype.A)
                resp, _ = dns.query.udp_with_fallback(q, server, timeout=self.timeout, source=source_ip)
            except (dns.exception.DNSException, OSError) as exc:
                errors.append(f"{server}: {exc}")
                continue
            addrs: list[str] = []
            ttl = self.cache_seconds
            for rrset in resp.answer:
                if rrset.rdtype == dns.rdatatype.A:
                    ttl = min(ttl, rrset.ttl or ttl)
                    addrs += [r.address for r in rrset]
            if addrs:
                with self._lock:
                    self._cache[key] = (now + max(ttl, 5), addrs)
                return addrs
            errors.append(f"{server}: no A record")
        raise ResolveError(f"cannot resolve {host}: " + "; ".join(errors))
