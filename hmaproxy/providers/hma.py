"""HideMyAss (HMA) provider: HMA .ovpn files (manual OpenVPN setup)."""

from __future__ import annotations

from pathlib import Path

from .base import Server, VpnProvider, split_ovpn_name


class HmaProvider(VpnProvider):
    name = "hma"
    display_name = "HMA"
    user_env = "HMA_USER"
    pass_env = "HMA_PASS"

    def parse_server(self, path: Path) -> Server:
        words, protocol = split_ovpn_name(path.name)
        if not words:
            words = [path.stem]
        country = words[0]
        return Server(
            filename=path.name,
            path=path,
            country=country,
            location=" ".join(words),
            protocol=protocol,
        )
