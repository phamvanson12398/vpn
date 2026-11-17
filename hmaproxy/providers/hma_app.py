"""HMA through the official HMA app ("app" mode): one proxy, no .ovpn needed.

The user connects with the HMA app as usual; the proxy binds its outgoing
connections to the IP of the HMA app's virtual adapter, so traffic only ever
leaves through that VPN. One VPN connection on the PC = one proxy / one IP.
"""

from __future__ import annotations

from pathlib import Path

from .base import Server, VpnProvider

APP_SERVER_FILE = "(ung-dung-hma)"


class HmaAppProvider(VpnProvider):
    name = "hma-app"
    display_name = "HMA – dùng ứng dụng HMA (1 proxy)"
    mode = "app"
    user_env = ""
    pass_env = ""
    #: default substrings to recognise the HMA app's adapter (name or description)
    adapter_patterns = ["hma", "hidemyass", "privax"]

    def parse_server(self, path: Path) -> Server:  # not used in app mode
        return Server(filename=path.name, path=path, country="HMA", location="HMA", protocol="")

    def app_server(self) -> Server:
        return Server(filename=APP_SERVER_FILE, path=Path("."), country="HMA app", location="HMA app", protocol="")
