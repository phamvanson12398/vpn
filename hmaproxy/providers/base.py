"""VPN provider abstraction.

The core (config, docker_ctl, health, recover) only talks to this interface, so
adding another VPN vendor means adding one subclass and registering it in
``providers/__init__.py`` - nothing in the core changes.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

# Tokens in .ovpn file names that describe the transport, not the location.
_PROTO_TOKENS = {"tcp", "udp", "tcp443", "udp1194", "ovpn", "openvpn"}


@dataclass(frozen=True)
class Server:
    """One VPN endpoint, i.e. one .ovpn file."""

    filename: str
    path: Path
    country: str
    location: str  # human readable, e.g. "Japan Tokyo"
    protocol: str  # "tcp", "udp" or ""

    @property
    def country_key(self) -> str:
        return normalize_country(self.country)


def normalize_country(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def split_ovpn_name(filename: str) -> tuple[list[str], str]:
    """Split ``Japan.Tokyo.TCP.ovpn`` into (["Japan", "Tokyo"], "tcp")."""
    stem = filename[: -len(".ovpn")] if filename.lower().endswith(".ovpn") else filename
    tokens = [tok for tok in re.split(r"[.\s_-]+", stem) if tok]
    protocol = ""
    words: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in _PROTO_TOKENS:
            if low.startswith("tcp"):
                protocol = "tcp"
            elif low.startswith("udp"):
                protocol = "udp"
            continue
        words.append(tok)
    return words, protocol


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str


class VpnProvider(ABC):
    """Interface every VPN vendor implements."""

    #: name used in config.yaml ``provider:``
    name: str = ""
    display_name: str = ""
    #: "openvpn" = this tool runs one openvpn.exe per proxy; "app" = use the vendor's own app (1 proxy)
    mode: str = "openvpn"
    #: environment variables holding the VPN username / password
    user_env: str = ""
    pass_env: str = ""

    @abstractmethod
    def parse_server(self, path: Path) -> Server:
        """Build a Server from one .ovpn file."""

    def list_servers(self, ovpn_dir: Path) -> list[Server]:
        if not ovpn_dir.is_dir():
            return []
        files = sorted(p for p in ovpn_dir.iterdir() if p.is_file() and p.suffix.lower() == ".ovpn")
        return [self.parse_server(p) for p in files]

    def alternatives(self, servers: list[Server], current: Server, exclude: set[str] | None = None) -> list[Server]:
        """Other servers in the same country, preferring the same protocol."""
        exclude = set(exclude or ()) | {current.filename}
        same = [s for s in servers if s.country_key == current.country_key and s.filename not in exclude]
        same.sort(key=lambda s: (s.protocol != current.protocol, s.filename))
        return same
