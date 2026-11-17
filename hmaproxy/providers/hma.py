"""HideMyAss (HMA) provider: gluetun "custom" OpenVPN mode with HMA .ovpn files."""

from __future__ import annotations

from pathlib import Path

from .base import Server, VpnProvider, split_ovpn_name


class HmaProvider(VpnProvider):
    name = "hma"
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

    def gluetun_env(self, ovpn_container_path: str) -> dict[str, str]:
        return {
            "VPN_SERVICE_PROVIDER": "custom",
            "VPN_TYPE": "openvpn",
            "OPENVPN_CUSTOM_CONFIG": ovpn_container_path,
        }
