"""Command line interface.

The VPN tunnels live inside the running program (started with ``start.bat`` /
``hmaproxy web``). ``init``, ``validate`` and ``web`` work locally; all other
commands talk to the running program through its local web API.

    hmaproxy web [--port N] [--no-browser]
    hmaproxy init | validate
    hmaproxy create [ids...] [--reshuffle]
    hmaproxy start|stop|restart|remove [id|all]
    hmaproxy list
    hmaproxy check [id|all]
    hmaproxy export --format plain|http|socks5|json [--host IP] [--no-auth] [--all] [-o file]
    hmaproxy logs <id|manager> [--tail N]

Exit codes: 0 ok, 1 error, 2 config, 3 system (OpenVPN/admin), 4 credentials,
5 not found, 6 health check failed, 7 program not running.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from . import __version__
from .errors import HmaProxyError, NotFoundError
from .i18n import set_language, t
from .logging_setup import redact


def _out(text: str = "") -> None:
    print(redact(text), flush=True)


def _err(text: str) -> None:
    print(redact(text), file=sys.stderr, flush=True)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(str(c))) for w, c in zip(widths, r)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join([line, sep, *body])


def _fmt_uptime(sec) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d{h:02d}h" if d else (f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hmaproxy", description="HMA Proxy Manager")
    p.add_argument("--config", "-c", default=os.environ.get("HMAPROXY_CONFIG", "config.yaml"))
    p.add_argument("--lang", choices=["vi", "en"], help="ngôn ngữ thông báo / message language")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="chạy chương trình: VPN + proxy + giao diện web")
    w.add_argument("--port", type=int)
    w.add_argument("--bind")
    w.add_argument("--no-browser", action="store_true")

    sub.add_parser("init", help="tạo config.yaml, .env, ovpn/")
    sub.add_parser("validate", help="kiểm tra cấu hình")

    c = sub.add_parser("create", help="tạo và chạy các proxy trong cấu hình")
    c.add_argument("ids", nargs="*")
    c.add_argument("--reshuffle", action="store_true", help="chế độ random: chọn lại server")

    for name in ("start", "stop", "restart", "remove", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("target", nargs="?", default="all")

    sub.add_parser("list")

    e = sub.add_parser("export")
    e.add_argument("--format", "-f", default="http", choices=["plain", "ip", "http", "socks5", "json"])
    e.add_argument("--host")
    e.add_argument("--no-auth", action="store_true")
    e.add_argument("--all", action="store_true", help="kể cả proxy chưa khỏe")
    e.add_argument("--output", "-o")

    lg = sub.add_parser("logs")
    lg.add_argument("id", help="id proxy, hoặc 'manager'")
    lg.add_argument("--tail", type=int, default=100)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_language(args.lang)
    try:
        return _dispatch(args)
    except HmaProxyError as exc:
        _err(f"error: {exc}" if (args.lang or "") == "en" else f"Lỗi: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _err(t("interrupted"))
        return 130


class ApiClient:
    """Talks to the running program's local web API."""

    def __init__(self, config_path: Path):
        web = {}
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            web = data.get("web") or {}
            if not os.environ.get("HMAPROXY_LANG"):
                set_language(data.get("language"))
        except (OSError, yaml.YAMLError):
            pass
        bind = str(web.get("bind", "127.0.0.1"))
        host = "127.0.0.1" if bind in ("0.0.0.0", "::", "localhost") else bind
        self.base = f"http://{host}:{int(web.get('port', 8800))}"
        self.password = str(web.get("password") or "")

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 600):
        headers = {"X-HMA-Request": "1", "Content-Type": "application/json"}
        if self.password:
            headers["Authorization"] = "Basic " + base64.b64encode(f"admin:{self.password}".encode()).decode()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a system proxy
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                msg = json.loads(raw).get("error") or raw
            except ValueError:
                msg = raw or str(exc)
            raise (NotFoundError(msg) if exc.code == 404 else HmaProxyError(msg)) from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise HmaProxyError(t("web_not_running", url=self.base), exit_code=7) from exc


def _print_results(res: dict) -> int:
    for r in res.get("results", []):
        (_out if r.get("ok") else _err)(r.get("message") or f"{r.get('id')}: {r.get('error')}")
    if res.get("ok") is False and not res.get("results"):
        _err(res.get("error") or "error")
    return 0 if res.get("ok", True) else 1


def _dispatch(args) -> int:
    cmd = args.cmd
    config_path = Path(args.config)

    if cmd == "web":
        from .web import serve

        return serve(config_path, bind=args.bind, port=args.port, open_browser=False if args.no_browser else None)

    if cmd == "init":
        from .templates import init_project

        for path, created in init_project(config_path.resolve().parent):
            _out(t("init_created" if created else "init_exists", path=path))
        return 0

    if cmd == "validate":
        from .config import env_file_warning, load_config

        cfg = load_config(config_path)
        if not args.lang:
            set_language(cfg.language)
        for w in cfg.warnings:
            _err(w)
        warn = env_file_warning(cfg.env_file)
        if warn:
            _err(warn)
        _out(t("config_ok", count=len(cfg.proxies), max=cfg.max_connections))
        for p in cfg.proxies:
            _out(f"  {p.id:<12} {p.country:<16} {p.ovpn:<40} http:{p.http_port}" + (f" socks5:{p.socks_port}" if p.socks_port else ""))
        return 0

    api = ApiClient(config_path)
    if args.lang:
        set_language(args.lang)

    if cmd == "create":
        return _print_results(api.call("POST", "/api/create", {"ids": args.ids or None, "reshuffle": args.reshuffle}))

    if cmd in ("start", "stop", "restart", "remove"):
        return _print_results(api.call("POST", f"/api/proxies/{args.target}/{cmd}", {}))

    if cmd == "check":
        res = api.call("POST", f"/api/proxies/{args.target}/check", {})
        _out(t("real_ip", ip=res.get("real_ip")) if res.get("real_ip") else t("real_ip_unknown", error="-"))
        for r in res["results"]:
            if r["ok"]:
                _out(t("check_ok", id=r["id"], ip=r["ip"], ms=r["latency_ms"]))
                for other in r.get("duplicate_of") or []:
                    _err("  " + t("ip_duplicate", id=r["id"], ip=r["ip"], other=other))
            else:
                _err(t("check_fail", id=r["id"], error=r["error"]))
            if r.get("socks_ok") is False:
                _err("  " + t("socks_fail", error=r.get("socks_error")))
        return 0 if res.get("ok") else 6

    if cmd == "list":
        st = api.call("GET", "/api/status")
        if st.get("config_error"):
            _err(st["config_error"])
        rows = st.get("proxies") or []
        if not rows:
            _out(t("no_managed"))
            return 0
        table = []
        for r in rows:
            ports = str(r["http_port"]) + (f"/{r['socks_port']}" if r.get("socks_port") else "")
            table.append([r["id"], r["country"], ports, r["status"], r["health"], r.get("ip") or "-",
                          _fmt_uptime(r.get("uptime"))])
        _out(_table(["ID", "COUNTRY", "PORT(HTTP/SOCKS)", "VPN", "HEALTH", "EXIT IP", "UPTIME"], table))
        return 0

    if cmd == "export":
        fmt = "plain" if args.format == "ip" else args.format
        q = {"format": fmt, "auth": "0" if args.no_auth else "1", "only_healthy": "0" if args.all else "1"}
        if args.host:
            q["host"] = args.host
        text = api.call("GET", "/api/export?" + urllib.parse.urlencode(q))
        if not isinstance(text, str):
            text = json.dumps(text, indent=2, ensure_ascii=False)
        if not text.strip() or text.strip() == "[]":
            _err(t("export_empty"))
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)  # proxy credentials are intended here; the HMA password never is
        return 0

    if cmd == "logs":
        path = "/api/logs" if args.id == "manager" else f"/api/proxies/{args.id}/logs"
        _out(api.call("GET", f"{path}?tail={args.tail}"))
        return 0

    raise HmaProxyError(f"unknown command {cmd}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
