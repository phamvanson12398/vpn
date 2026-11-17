"""ExpressVPN: manual OpenVPN configuration.

Files come from the ExpressVPN website: Sign in -> "Set up other devices" ->
"Manual Configuration" -> OpenVPN. That page also shows the dedicated
OpenVPN username/password (NOT the account email/password).
File names look like ``my_expressvpn_japan_-_tokyo_udp.ovpn``.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import Server, VpnProvider


class ExpressVpnProvider(VpnProvider):
    name = "expressvpn"
    display_name = "ExpressVPN"
    user_env = "EXPRESSVPN_USER"
    pass_env = "EXPRESSVPN_PASS"

    def parse_server(self, path: Path) -> Server:
        stem = path.name[: -len(".ovpn")] if path.name.lower().endswith(".ovpn") else path.name
        low = stem.lower()
        protocol = ""
        m = re.search(r"[_\s.-](udp|tcp)$", low)
        if m:
            protocol = m.group(1)
            stem = stem[: m.start()]
        stem = re.sub(r"^my[_\s-]*expressvpn[_\s-]*", "", stem, flags=re.IGNORECASE)
        parts = [p for p in re.split(r"_-_|\s-\s", stem) if p]
        words = [re.sub(r"[_\s]+", " ", p).strip() for p in parts]
        words = [(w.upper() if len(w) <= 3 else w.title()) if w.islower() else w for w in words if w] or [path.stem]
        country = words[0]
        return Server(filename=path.name, path=path, country=country, location=" - ".join(words), protocol=protocol)
