import random

import pytest

from hmaproxy.config import load_config, load_credentials, pick_random_servers
from hmaproxy.errors import ConfigError, CredentialError
from hmaproxy.providers import get_provider
from hmaproxy.providers.base import split_ovpn_name


def test_loads_valid_config(write_config):
    cfg = load_config(write_config(base_port=8881))
    assert [p.id for p in cfg.proxies] == ["jp", "sg", "us"]
    assert [p.http_port for p in cfg.proxies] == [8881, 8882, 8883]
    assert cfg.openvpn.driver == "tap-windows6" and cfg.dns.servers[0] == "1.1.1.1"
    assert cfg.proxies[0].country == "Japan"
    assert all(p.socks_port is None for p in cfg.proxies)
    assert cfg.warnings == []


def test_rejects_more_than_max_connections(write_config):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(max_connections=2))
    assert "3 proxies" in str(exc.value) and "max_connections = 2" in str(exc.value)
    assert exc.value.exit_code == 2


def test_missing_ovpn_file(write_config):
    with pytest.raises(ConfigError, match="Nope.ovpn"):
        load_config(write_config(proxies=[{"id": "x", "ovpn": "Nope.ovpn"}]))


def test_duplicate_ports_rejected(write_config):
    with pytest.raises(ConfigError, match="8881"):
        load_config(write_config(base_port=8881, proxies=[
            {"id": "a", "ovpn": "Japan.Tokyo.TCP.ovpn"},
            {"id": "b", "ovpn": "Singapore.TCP.ovpn", "port": 8881},
        ]))


def test_socks_port_clash_with_web(write_config):
    with pytest.raises(ConfigError, match="8800"):
        load_config(write_config(socks5={"enabled": True, "base_port": 8800}))


def test_duplicate_ids_rejected(write_config):
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(write_config(proxies=[
            {"id": "a", "ovpn": "Japan.Tokyo.TCP.ovpn"},
            {"id": "a", "ovpn": "Singapore.TCP.ovpn"},
        ]))


def test_socks_ports_assigned(write_config):
    cfg = load_config(write_config(socks5={"enabled": True, "base_port": 1081}))
    assert [p.socks_port for p in cfg.proxies] == [1081, 1082, 1083]


def test_public_bind_without_auth_warns(write_config):
    cfg = load_config(write_config(bind_address="0.0.0.0"))
    assert cfg.warnings and "0.0.0.0" in cfg.warnings[0]


def test_auth_requires_password(write_config):
    with pytest.raises(ConfigError):
        load_config(write_config(proxy_auth={"enabled": True, "user": "u", "password": ""}))


def test_unknown_key_rejected(write_config):
    with pytest.raises(ConfigError, match="health_check.intervall"):
        load_config(write_config(health_check={"intervall": 5}))


def test_random_selection_distinct_countries(write_config):
    cfg = load_config(write_config(proxies=None, selection={"mode": "random", "count": 4}))
    countries = [p.country for p in cfg.proxies]
    assert len(countries) == 4 and len(set(countries)) == 4


def test_random_selection_too_many(write_config):
    with pytest.raises(ConfigError):
        load_config(write_config(proxies=None, selection={"mode": "random", "count": 5}))  # only 4 countries


def test_random_selection_uses_saved_plan(write_config):
    plan = [{"id": "jp", "ovpn": "Japan.Osaka.TCP.ovpn", "country": "Japan"}]
    cfg = load_config(write_config(proxies=None, selection={"mode": "random", "count": 1}), saved_plan=plan)
    assert cfg.proxies[0].ovpn == "Japan.Osaka.TCP.ovpn"


def test_pick_random_respects_country_filter(write_config):
    cfg = load_config(write_config())
    cfg.selection.countries = ["usa"]
    cfg.selection.distinct_countries = False
    cfg.selection.count = 2
    picked = pick_random_servers(cfg, random.Random(1))
    assert {p["country"] for p in picked} == {"USA"}
    assert len({p["id"] for p in picked}) == 2


def test_credentials_from_env_file_and_env(write_config, monkeypatch):
    cfg = load_config(write_config())
    assert load_credentials(cfg).user == "vpnuser"
    monkeypatch.setenv("HMA_USER", "override")
    assert load_credentials(cfg).user == "override"


def test_missing_credentials(write_config, project):
    (project / ".env").write_text("HMA_USER=\n")
    cfg = load_config(write_config())
    with pytest.raises(CredentialError) as exc:
        load_credentials(cfg)
    assert exc.value.exit_code == 4


def test_ovpn_name_parsing():
    assert split_ovpn_name("Japan.Tokyo.TCP.ovpn") == (["Japan", "Tokyo"], "tcp")
    assert split_ovpn_name("UnitedStates.ovpn") == (["UnitedStates"], "")
    s = get_provider("hma").parse_server(__import__("pathlib").Path("USA_New-York.udp.ovpn"))
    assert s.country == "USA" and s.protocol == "udp" and s.location == "USA New York"


def test_bad_driver_and_dns(write_config):
    with pytest.raises(ConfigError, match="openvpn.driver"):
        load_config(write_config(openvpn={"driver": "tap"}))
    with pytest.raises(ConfigError, match="dns.servers"):
        load_config(write_config(dns={"servers": ["dns.google"]}))


def test_empty_proxy_list_is_allowed(write_config):
    cfg = load_config(write_config(proxies=[]))
    assert cfg.proxies == [] and cfg.warnings


def test_unknown_provider(write_config):
    with pytest.raises(ConfigError, match="Unsupported"):
        load_config(write_config(provider="nordvpn"))
