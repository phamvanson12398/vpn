"""Exit-IP checks through each proxy (FR-4)."""

from __future__ import annotations

import ipaddress
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

from .config import AppConfig
from .i18n import t
from .logging_setup import redact


@dataclass
class CheckResult:
    proxy_id: str
    ok: bool
    ip: str | None = None
    error: str | None = None
    latency_ms: int | None = None
    socks_ok: bool | None = None   # None = SOCKS5 not configured / not checked
    socks_error: str | None = None
    leak: bool = False
    duplicate_of: list[str] = field(default_factory=list)


def _auth_prefix(cfg: AppConfig) -> str:
    if not cfg.proxy_auth.enabled:
        return ""
    return f"{quote(cfg.proxy_auth.user, safe='')}:{quote(cfg.proxy_auth.password, safe='')}@"


def http_proxy_url(cfg: AppConfig, port: int, host: str | None = None) -> str:
    return f"http://{_auth_prefix(cfg)}{host or cfg.connect_host}:{port}"


def socks_proxy_url(cfg: AppConfig, port: int, host: str | None = None, remote_dns: bool = True) -> str:
    # socks5h = hostname is resolved by the proxy (through the VPN), not locally.
    scheme = "socks5h" if remote_dns else "socks5"
    return f"{scheme}://{_auth_prefix(cfg)}{host or cfg.connect_host}:{port}"


def _urls(cfg: AppConfig) -> list[str]:
    urls = [cfg.health.ip_check_url]
    urls += [u for u in cfg.health.fallback_urls if u not in urls]
    return urls


def _parse_ip(text: str) -> str:
    candidate = text.strip().split()[0] if text.strip() else ""
    ipaddress.ip_address(candidate)  # raises ValueError
    return candidate


def fetch_ip(urls: list[str], proxy: str | None, timeout: float, session: requests.Session | None = None) -> str:
    """Return the public IP seen through ``proxy`` (or directly when None)."""
    sess = session or requests.Session()
    sess.trust_env = False  # never let HTTP(S)_PROXY from the environment interfere
    proxies = {"http": proxy, "https": proxy} if proxy else {"http": None, "https": None}
    errors = []
    for url in urls:
        try:
            resp = sess.get(url, proxies=proxies, timeout=timeout, headers={"User-Agent": "curl/8"})
            resp.raise_for_status()
            return _parse_ip(resp.text)
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {_short(exc)}")
    raise ConnectionError(redact("; ".join(errors)))


def _short(exc: Exception) -> str:
    text = str(exc)
    # requests' messages are long nested reprs; keep the useful tail.
    if len(text) > 160:
        text = text[:157] + "..."
    return text


def get_real_ip(cfg: AppConfig) -> str:
    return fetch_ip(_urls(cfg), None, cfg.health.timeout_seconds)


def check_proxy(cfg: AppConfig, proxy_id: str, http_port: int, socks_port: int | None,
                real_ip: str | None = None) -> CheckResult:
    urls = _urls(cfg)
    timeout = cfg.health.timeout_seconds
    start = time.monotonic()
    result = CheckResult(proxy_id=proxy_id, ok=False)
    try:
        ip = fetch_ip(urls, http_proxy_url(cfg, http_port), timeout)
        result.ip = ip
        result.latency_ms = int((time.monotonic() - start) * 1000)
        if real_ip and ip == real_ip:
            result.leak = True
            result.error = t("ip_leak", ip=ip)
        else:
            result.ok = True
    except ConnectionError as exc:
        result.error = str(exc)

    if socks_port is not None:
        try:
            sip = fetch_ip(urls, socks_proxy_url(cfg, socks_port), timeout)
            if real_ip and sip == real_ip:
                result.socks_ok = False
                result.leak = True
                result.socks_error = t("ip_leak", ip=sip)
            else:
                result.socks_ok = True
                if result.ip is None:
                    result.ip = sip
        except ConnectionError as exc:
            result.socks_ok = False
            result.socks_error = str(exc)
    return result


def check_many(cfg: AppConfig, targets: list[tuple[str, int, int | None]], real_ip: str | None) -> list[CheckResult]:
    """Check several proxies in parallel, then flag proxies sharing the same exit IP."""
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(targets))) as pool:
        results = list(pool.map(lambda tgt: check_proxy(cfg, tgt[0], tgt[1], tgt[2], real_ip), targets))
    mark_duplicates(results)
    return results


def mark_duplicates(results: list[CheckResult]) -> None:
    by_ip: dict[str, list[str]] = {}
    for r in results:
        if r.ok and r.ip:
            by_ip.setdefault(r.ip, []).append(r.proxy_id)
    for r in results:
        if r.ok and r.ip:
            r.duplicate_of = [p for p in by_ip[r.ip] if p != r.proxy_id]
