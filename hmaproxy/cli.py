"""Command line interface.

    hmaproxy web                         # giao diện web (mặc định trên Windows)
    hmaproxy init
    hmaproxy validate
    hmaproxy create [ids...] [--wait] [--reshuffle]
    hmaproxy start|stop|restart|remove [id|all]
    hmaproxy list
    hmaproxy check [id|all]
    hmaproxy export --format plain|http|socks5|json
    hmaproxy logs <id> [--tail N] [--follow] [--socks]
    hmaproxy monitor                     # chỉ giám sát, không có web
    hmaproxy gateway                     # chỉ chạy cổng xoay vòng

Exit codes: 0 ok, 1 generic error, 2 config, 3 docker, 4 credentials, 5 not found, 6 health check failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .config import env_file_warning
from .errors import HealthError, HmaProxyError
from .i18n import set_language, t
from .logging_setup import get_logger, redact, setup_logging


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
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="giao diện web + giám sát")
    w.add_argument("--port", type=int)
    w.add_argument("--bind")
    w.add_argument("--no-browser", action="store_true")

    sub.add_parser("init", help="tạo config.yaml, .env, ovpn/")
    sub.add_parser("validate", help="kiểm tra cấu hình")

    c = sub.add_parser("create", help="tạo container cho các proxy")
    c.add_argument("ids", nargs="*")
    c.add_argument("--wait", action="store_true", help="chờ tới khi proxy có IP")
    c.add_argument("--timeout", type=int, default=120)
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
    lg.add_argument("id")
    lg.add_argument("--tail", type=int, default=100)
    lg.add_argument("--follow", "-f", action="store_true")
    lg.add_argument("--socks", action="store_true")

    sub.add_parser("monitor", help="chạy giám sát + tự phục hồi (không web)")
    g = sub.add_parser("gateway", help="cổng proxy xoay vòng")
    g.add_argument("--type", choices=["http", "socks5"], default="http")
    g.add_argument("--port", type=int)
    g.add_argument("--mode", choices=["roundrobin", "random"])
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


def _manager(args):
    from .service import Manager

    mgr = Manager.from_path(args.config)
    if not args.lang:
        set_language(mgr.cfg.language)
    lc = mgr.cfg.log
    setup_logging(mgr.cfg.state_dir / lc.file, level=lc.level,
                  console_level="DEBUG" if args.verbose else ("INFO" if args.cmd in ("monitor", "gateway") else "WARNING"),
                  max_bytes=lc.max_bytes, backup_count=lc.backup_count)
    for w in mgr.cfg.warnings:
        _err(w)
    warn = env_file_warning(mgr.cfg.env_file)
    if warn:
        _err(warn)
    return mgr


def _print_results(results) -> int:
    for r in results:
        (_out if r.ok else _err)(r.message)
    return 0 if all(r.ok for r in results) else 1


def _dispatch(args) -> int:
    cmd = args.cmd
    if cmd == "web":
        from .web import serve

        return serve(Path(args.config), bind=args.bind, port=args.port, open_browser=False if args.no_browser else None)

    if cmd == "init":
        from .templates import init_project

        for path, created in init_project(Path(args.config).resolve().parent):
            _out(t("init_created" if created else "init_exists", path=path))
        return 0

    mgr = _manager(args)
    cfg = mgr.cfg

    if cmd == "validate":
        _out(t("config_ok", count=len(cfg.proxies), max=cfg.max_connections))
        for p in cfg.proxies:
            _out(f"  {p.id:<12} {p.country:<16} {p.ovpn:<40} http:{p.http_port}" + (f" socks5:{p.socks_port}" if p.socks_port else ""))
        return 0

    if cmd == "create":
        report = mgr.create(ids=args.ids or None, reshuffle=args.reshuffle)
        code = _print_results(report.results)
        if args.wait:
            ids = [r.id for r in report.results if r.ok]
            _out(t("waiting_ready", timeout=args.timeout))
            res = mgr.wait_ready(ids, timeout=args.timeout)
            for pid in ids:
                r = res.get(pid)
                if r and r.ok:
                    _out(t("ready", id=pid, ip=r.ip))
                else:
                    _err(t("not_ready", id=pid, timeout=args.timeout, error=r.error if r else "-"))
                    code = HealthError.exit_code
        return code

    if cmd in ("start", "stop", "restart", "remove"):
        return _print_results(getattr(mgr, cmd)(args.target))

    if cmd == "list":
        rows = mgr.rows()
        if not rows:
            _out(t("no_managed"))
            return 0
        table = []
        for r in rows:
            ports = str(r["http_port"]) + (f"/{r['socks_port']}" if r["socks_port"] else "")
            table.append([r["id"], r["country"], ports, r["status"], r["health"], r["ip"] or "-", _fmt_uptime(r["uptime"])])
        _out(_table(["ID", "COUNTRY", "PORT(HTTP/SOCKS)", "STATUS", "HEALTH", "EXIT IP", "UPTIME"], table))
        return 0

    if cmd == "check":
        real_ip, results = mgr.check(args.target)
        _out(t("real_ip", ip=real_ip) if real_ip else t("real_ip_unknown", error="-"))
        for r in results:
            if r.ok:
                _out(t("check_ok", id=r.proxy_id, ip=r.ip, ms=r.latency_ms))
                for other in r.duplicate_of:
                    _err("  " + t("ip_duplicate", id=r.proxy_id, ip=r.ip, other=other))
            else:
                _err(t("check_fail", id=r.proxy_id, error=r.error))
            if r.socks_ok is False:
                _err("  " + t("socks_fail", error=r.socks_error))
        return 0 if all(r.ok and r.socks_ok is not False for r in results) else HealthError.exit_code

    if cmd == "export":
        from .export import export_proxies

        fmt = "plain" if args.format == "ip" else args.format
        if fmt == "socks5" and not cfg.socks5.enabled:
            raise HmaProxyError(t("socks_disabled"), exit_code=2)
        rows = [r for r in mgr.rows() if r["created"]]
        text = export_proxies(cfg, rows, fmt, host=args.host, include_auth=not args.no_auth, only_healthy=not args.all)
        if not text.strip() or text.strip() == "[]":
            _err(t("export_empty"))
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)  # proxy credentials are intended here; HMA password never is
        return 0

    if cmd == "logs":
        from .errors import NotFoundError

        if args.id not in mgr.ctl.list_managed():
            raise NotFoundError(t("proxy_not_found", id=args.id))
        for chunk in mgr.ctl.logs(args.id, tail=args.tail, follow=args.follow, socks=args.socks):
            sys.stdout.write(redact(chunk))
            sys.stdout.flush()
        return 0

    if cmd == "monitor":
        from .monitor import Monitor

        mon = Monitor(mgr)
        _install_stop(mon.stop)
        if cfg.gateway.enabled:
            _start_gateways(mgr)
        mon.run()
        return 0

    if cmd == "gateway":
        from .gateway import Gateway

        kind = "socks" if args.type == "socks5" else "http"
        if kind == "socks" and not cfg.socks5.enabled:
            raise HmaProxyError(t("socks_disabled"), exit_code=2)
        port = args.port or (cfg.gateway.socks_port if kind == "socks" else cfg.gateway.http_port)
        gw = Gateway(cfg.bind_address, port, _cached_backends(mgr, kind), args.mode or cfg.gateway.mode)
        _out(t("gateway_listening", mode=gw.mode, bind=gw.bind, port=port, count=len(mgr.healthy_backends(kind))))
        asyncio.run(gw.serve_forever())
        return 0

    raise HmaProxyError(f"unknown command {cmd}")  # pragma: no cover


def _cached_backends(mgr, kind: str, ttl: float = 10.0):
    import time

    cache: dict = {"at": 0.0, "value": []}

    def get():
        if time.time() - cache["at"] > ttl:
            try:
                cache["value"] = mgr.healthy_backends(kind)
            except HmaProxyError as exc:
                get_logger("gateway").warning(str(exc))
            cache["at"] = time.time()
        return cache["value"]

    return get


def _start_gateways(mgr) -> None:
    from .gateway import Gateway, run_gateways_in_thread

    cfg = mgr.cfg
    gws = [Gateway(cfg.bind_address, cfg.gateway.http_port, _cached_backends(mgr, "http"), cfg.gateway.mode)]
    if cfg.socks5.enabled:
        gws.append(Gateway(cfg.bind_address, cfg.gateway.socks_port, _cached_backends(mgr, "socks"), cfg.gateway.mode))
    run_gateways_in_thread(gws)


def _install_stop(fn) -> None:
    def handler(signum, frame):
        fn()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover
            pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
