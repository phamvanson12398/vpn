from __future__ import annotations

from ..errors import ConfigError
from ..i18n import t
from .base import Credentials, Server, VpnProvider, normalize_country
from .expressvpn import ExpressVpnProvider
from .hma import HmaProvider

PROVIDERS: dict[str, type[VpnProvider]] = {
    HmaProvider.name: HmaProvider,
    ExpressVpnProvider.name: ExpressVpnProvider,
}


def get_provider(name: str) -> VpnProvider:
    cls = PROVIDERS.get((name or "").lower())
    if cls is None:
        raise ConfigError(t("unknown_provider", name=name, supported=", ".join(sorted(PROVIDERS))))
    return cls()


__all__ = ["Credentials", "Server", "VpnProvider", "PROVIDERS", "get_provider", "normalize_country"]
