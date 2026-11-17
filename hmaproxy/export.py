"""Export the proxy list in ready-to-use formats (FR-6)."""

from __future__ import annotations

import json
import socket
from urllib.parse import quote

from .config import AppConfig

FORMATS = ("plain", "http", "socks5", "json")


def public_host(cfg: AppConfig) -> str:
    """Host other programs should use to reach the proxies."""
    if cfg.bind_address not in ("0.0.0.0", "::"):
        return "127.0.0.1" if cfg.bind_address == "localhost" else cfg.bind_address
    # Bound to all interfaces: report this machine's LAN address.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def export_proxies(cfg: AppConfig, rows: list[dict], fmt: str, host: str | None = None,
                   include_auth: bool = True, only_healthy: bool = False) -> str:
    """``rows``: dicts with id, country, http_port, socks_port, ip, health, status."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    host = host or public_host(cfg)
    if only_healthy:
        rows = [r for r in rows if r.get("health") == "healthy"]
    auth = include_auth and cfg.proxy_auth.enabled
    user = quote(cfg.proxy_auth.user, safe="") if auth else ""
    pw = quote(cfg.proxy_auth.password, safe="") if auth else ""
    cred = f"{user}:{pw}@" if auth else ""

    if fmt == "json":
        items = []
        for r in rows:
            item = {
                "id": r["id"],
                "country": r.get("country"),
                "host": host,
                "http_port": r.get("http_port"),
                "http": f"http://{cred}{host}:{r['http_port']}" if r.get("http_port") else None,
                "socks5_port": r.get("socks_port"),
                "socks5": f"socks5://{cred}{host}:{r['socks_port']}" if r.get("socks_port") else None,
                "exit_ip": r.get("ip"),
                "health": r.get("health"),
                "status": r.get("status"),
            }
            if auth:
                item["username"] = cfg.proxy_auth.user
                item["password"] = cfg.proxy_auth.password
            items.append(item)
        return json.dumps(items, indent=2, ensure_ascii=False)

    lines = []
    for r in rows:
        if fmt == "plain":
            line = f"{host}:{r['http_port']}"
            if auth:
                line += f":{cfg.proxy_auth.user}:{cfg.proxy_auth.password}"
        elif fmt == "http":
            line = f"http://{cred}{host}:{r['http_port']}"
        else:  # socks5
            if not r.get("socks_port"):
                continue
            line = f"socks5://{cred}{host}:{r['socks_port']}"
        lines.append(line)
    return "\n".join(lines)
