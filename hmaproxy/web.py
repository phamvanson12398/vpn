"""Web UI + JSON API (stdlib only).

One process runs: the HTTP server for the UI, the health monitor thread and,
if enabled, the rotating gateways. Start it with ``python -m hmaproxy web``
(on Windows: ``start.bat``).

Security: listens on 127.0.0.1 by default; optional HTTP Basic password;
state-changing requests need the ``X-HMA-Request`` header (blocks cross-site
form posts) and, in loopback mode, the Host header must be local (blocks DNS
rebinding). The VPN password is write-only: no endpoint ever returns it.
"""

from __future__ import annotations

import base64
import hmac
import json
import re
import shutil
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from . import __version__
from .config import WebConfig, load_config, load_credentials, parse_env_file
from .errors import HmaProxyError, NotFoundError
from .export import FORMATS, export_proxies
from .gateway import Gateway, run_gateways_in_thread
from .i18n import set_language, t
from .logging_setup import get_logger, redact, register_secret, setup_logging
from .monitor import Monitor
from .providers import PROVIDERS, get_provider
from .service import Manager, system_ok
from .templates import init_project, set_env_values
from .vpn_ctl import VpnController

OVPN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,120}\.ovpn$", re.IGNORECASE)
MAX_BODY = 2 * 1024 * 1024


class WebApp:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.base_dir = self.config_path.parent
        if not self.config_path.exists():
            init_project(self.base_dir)
        self.web = self._read_web_settings()
        self.mgr: Manager | None = None
        self.ctl: VpnController | None = None   # lives for the whole process: tunnels survive config reloads
        self.config_error: str | None = None
        self.warnings: list[str] = []
        self.monitor: Monitor | None = None
        self.monitor_thread: threading.Thread | None = None
        self.op_lock = threading.Lock()
        self._backend_cache: dict[str, tuple[float, list]] = {}
        self.gateways_started = False
        self.log = get_logger("web")
        self.reload()

    # ------------------------------------------------------------------ config
    def _read_web_settings(self) -> WebConfig:
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            web = data.get("web") or {}
            return WebConfig(**{k: v for k, v in web.items() if k in WebConfig.__dataclass_fields__})
        except Exception:  # noqa: BLE001 - fall back to safe defaults
            return WebConfig()

    def reload(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        try:
            cfg = self._load_cfg()
        except HmaProxyError as exc:
            self.mgr = None
            self.config_error = str(exc)
            self.warnings = []
            setup_logging(self.base_dir / ".hmaproxy" / "logs" / "hmaproxy.log", console_level="INFO")
            self.log.error(self.config_error)
            return
        first = self.ctl is None
        if first:
            self.ctl = VpnController(cfg, self._store_for(cfg), credentials=self._credentials)
        else:
            self.ctl.cfg = cfg
        mgr = Manager(cfg, ctl=self.ctl, store=self.ctl.store)
        self.mgr = mgr
        self.config_error = None
        self.warnings = list(mgr.cfg.warnings)
        set_language(mgr.cfg.language)
        lc = mgr.cfg.log
        setup_logging(mgr.cfg.state_dir / lc.file, level=lc.level, console_level="INFO",
                      max_bytes=lc.max_bytes, backup_count=lc.backup_count)
        self._register_env_secret()
        for w in self.warnings:
            self.log.warning(w)
        if first:
            try:
                started = self.ctl.restore()
                if started:
                    self.log.info("restored proxies: %s", ", ".join(started))
            except HmaProxyError as exc:
                self.log.error(str(exc))
        self.monitor = Monitor(mgr)
        self.monitor_thread = self.monitor.run_in_thread()
        self._start_gateways()

    def _load_cfg(self):
        from .state import StateStore

        base = load_config(self.config_path)
        plan = StateStore(base.state_dir).load().get("plan")
        if plan and base.selection.mode == "random":
            return load_config(self.config_path, saved_plan=plan)
        return base

    def _store_for(self, cfg):
        from .state import StateStore

        return StateStore(cfg.state_dir)

    def _credentials(self):
        assert self.mgr is not None
        creds = load_credentials(self.mgr.cfg)
        register_secret(creds.password)
        return creds

    def shutdown(self) -> None:
        if self.monitor:
            self.monitor.stop()
        if self.ctl:
            self.log.info("stopping all VPN tunnels ...")
            self.ctl.shutdown()

    def _register_env_secret(self) -> None:
        if self.mgr is None:
            return
        values = parse_env_file(self.mgr.cfg.env_file)
        register_secret(values.get(self.mgr.cfg.provider.pass_env))

    def _start_gateways(self) -> None:
        if self.gateways_started or self.mgr is None or not self.mgr.cfg.gateway.enabled:
            return
        cfg = self.mgr.cfg
        gws = [Gateway(cfg.bind_address, cfg.gateway.http_port, lambda: self.backends("http"), cfg.gateway.mode,
                       name="gateway-http")]
        if cfg.socks5.enabled:
            gws.append(Gateway(cfg.bind_address, cfg.gateway.socks_port, lambda: self.backends("socks"),
                               cfg.gateway.mode, name="gateway-socks"))
        run_gateways_in_thread(gws)
        self.gateways_started = True
        for g in gws:
            self.log.info(t("gateway_listening", mode=g.mode, bind=g.bind, port=g.port, count="?"))

    def backends(self, kind: str) -> list[tuple[str, int]]:
        cached = self._backend_cache.get(kind)
        if cached and time.time() - cached[0] < 10:
            return cached[1]
        mgr = self.mgr
        value = mgr.healthy_backends(kind) if mgr else []
        self._backend_cache[kind] = (time.time(), value)
        return value

    def require_mgr(self) -> Manager:
        if self.mgr is None:
            raise HmaProxyError(self.config_error or "config not loaded", exit_code=2)
        return self.mgr

    # ------------------------------------------------------------------ API impl
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": __version__,
            "config_path": str(self.config_path),
            "config_error": self.config_error,
            "warnings": self.warnings,
            "credentials_set": False,
            "system": {"ok": False, "error": None},
            "proxies": [],
            "ovpn_files": [],
            "settings": None,
            "monitor": None,
            "real_ip": None,
        }
        mgr = self.mgr
        ovpn_dir = mgr.cfg.ovpn_dir if mgr else self.base_dir / "ovpn"
        out["ovpn_dir"] = str(ovpn_dir)
        out["ovpn_files"] = self._ovpn_files(ovpn_dir)
        env_path = mgr.cfg.env_file if mgr else self.base_dir / ".env"
        env = parse_env_file(env_path)
        prov = self._provider()
        out["provider"] = {"name": prov.name, "display": prov.display_name, "mode": prov.mode,
                           "choices": [{"name": k, "display": v().display_name} for k, v in sorted(PROVIDERS.items())]}
        if prov.mode == "app":
            from .winutil import list_adapters, match_adapter

            adapters = list_adapters()
            mgr_patterns = (self.mgr.cfg.adapter_patterns() if self.mgr else list(prov.adapter_patterns))
            out["app"] = {"adapters": adapters, "patterns": mgr_patterns,
                          "matched": match_adapter(adapters, mgr_patterns)}
        out["credentials_set"] = prov.mode == "app" or bool(env.get(prov.user_env) and env.get(prov.pass_env))
        out["vpn_user"] = env.get(prov.user_env, "")
        if mgr is None:
            return out
        cfg = mgr.cfg
        out["settings"] = {
            "provider": cfg.provider_name,
            "max_connections": cfg.max_connections,
            "bind_address": cfg.bind_address,
            "base_port": cfg.base_port,
            "socks5": cfg.socks5.enabled,
            "proxy_auth": cfg.proxy_auth.enabled,
            "gateway": cfg.gateway.enabled,
            "gateway_http_port": cfg.gateway.http_port,
            "gateway_socks_port": cfg.gateway.socks_port,
            "interval": cfg.health.interval_seconds,
            "selection": cfg.selection.mode,
            "language": cfg.language,
        }
        ok, err, info = system_ok(mgr)
        out["system"] = {"ok": ok, "error": redact(err) if err else None, **info}
        out["proxies"] = mgr.rows()
        real = mgr.store.load().get("real_ip") or {}
        out["real_ip"] = real.get("ip")
        if self.monitor:
            out["monitor"] = {"running": bool(self.monitor_thread and self.monitor_thread.is_alive()),
                              "last_tick": self.monitor.last_tick, "last_error": self.monitor.last_error}
        return out

    def _ovpn_files(self, ovpn_dir: Path) -> list[dict]:
        if not ovpn_dir.is_dir():
            return []
        provider = self._provider()
        return [{"filename": s.filename, "country": s.country, "location": s.location, "protocol": s.protocol}
                for s in provider.list_servers(ovpn_dir)]

    def action(self, action: str, target: str, body: dict) -> dict:
        mgr = self.require_mgr()
        with self.op_lock:
            if action == "create":
                ids = body.get("ids") or None
                report = mgr.create(ids=ids, reshuffle=bool(body.get("reshuffle")))
                if self.monitor:
                    self.monitor.recovery.cfg = mgr.cfg
                results = [r.__dict__ for r in report.results]
            elif action == "check":
                real_ip, checks = mgr.check(target)
                return {"ok": all(c.ok for c in checks), "real_ip": real_ip, "results": [
                    {"id": c.proxy_id, "ok": c.ok, "ip": c.ip, "error": c.error, "latency_ms": c.latency_ms,
                     "socks_ok": c.socks_ok, "socks_error": c.socks_error, "leak": c.leak,
                     "duplicate_of": c.duplicate_of} for c in checks]}
            elif action in ("start", "stop", "restart", "remove"):
                results = [r.__dict__ for r in getattr(mgr, action)(target)]
            else:
                raise NotFoundError(f"unknown action {action}")
        self._backend_cache.clear()
        if self.monitor:
            self.monitor.poke()
        return {"ok": all(r["ok"] for r in results), "results": results}

    def save_config(self, text: str) -> dict:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return {"ok": False, "error": t("config_invalid_yaml", error=exc)}
        if not isinstance(data, dict):
            return {"ok": False, "error": t("config_invalid_yaml", error="top level must be a mapping")}
        # Validate on a temp copy first so a bad edit never replaces a good file.
        tmp = self.config_path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            load_config(tmp)
            error = None
        except HmaProxyError as exc:
            error = str(exc)
        finally:
            tmp.unlink(missing_ok=True)
        if self.config_path.exists():
            shutil.copy2(self.config_path, self.config_path.with_suffix(".yaml.bak"))
        self.config_path.write_text(text, encoding="utf-8")
        self.web = self._read_web_settings()
        self.reload()
        return {"ok": error is None, "error": error, "saved": True}

    def add_proxy(self, body: dict) -> dict:
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        proxies = data.setdefault("proxies", []) or []
        data["proxies"] = proxies
        entry = {"id": str(body.get("id", "")).strip().lower(), "ovpn": str(body.get("ovpn", "")).strip()}
        if body.get("port"):
            entry["port"] = int(body["port"])
        if any(p.get("id") == entry["id"] for p in proxies if isinstance(p, dict)):
            return {"ok": False, "error": t("duplicate_id", id=entry["id"])}
        proxies.append(entry)
        return self.save_config(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def delete_proxy_from_config(self, proxy_id: str) -> dict:
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        data["proxies"] = [p for p in (data.get("proxies") or []) if not (isinstance(p, dict) and p.get("id") == proxy_id)]
        return self.save_config(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def save_credentials(self, body: dict) -> dict:
        user = str(body.get("user", "")).strip()
        password = str(body.get("password", ""))
        prov = self._provider()
        if not user or not password:
            return {"ok": False, "error": t("missing_credentials", names=f"{prov.user_env}, {prov.pass_env}")}
        if "\n" in user or "\n" in password:
            return {"ok": False, "error": "invalid characters"}
        register_secret(password)
        env_path = self.mgr.cfg.env_file if self.mgr else self.base_dir / ".env"
        set_env_values(env_path, {prov.user_env: user, prov.pass_env: password})
        self.log.info("VPN credentials updated (user=%s)", user)
        return {"ok": True}

    def upload_ovpn(self, body: dict) -> dict:
        name = Path(str(body.get("filename", ""))).name
        content = str(body.get("content", ""))
        if not OVPN_NAME_RE.match(name):
            return {"ok": False, "error": f"invalid file name: {name!r}"}
        if "remote " not in content and "<connection>" not in content:
            return {"ok": False, "error": f"{name}: not an OpenVPN config (no 'remote' line)"}
        ovpn_dir = self.mgr.cfg.ovpn_dir if self.mgr else self.base_dir / "ovpn"
        ovpn_dir.mkdir(parents=True, exist_ok=True)
        (ovpn_dir / name).write_text(content.replace("\r\n", "\n"), encoding="utf-8")
        return {"ok": True, "filename": name}

    def delete_ovpn(self, name: str) -> dict:
        name = Path(name).name
        ovpn_dir = self.mgr.cfg.ovpn_dir if self.mgr else self.base_dir / "ovpn"
        path = ovpn_dir / name
        if not OVPN_NAME_RE.match(name) or not path.is_file():
            raise NotFoundError(name)
        path.unlink()
        return {"ok": True}

    def _provider(self):
        if self.mgr is not None:
            return self.mgr.cfg.provider
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            return get_provider(str(data.get("provider", "hma")))
        except Exception:  # noqa: BLE001
            return get_provider("hma")

    def set_provider(self, body: dict) -> dict:
        name = str(body.get("provider", "")).lower()
        if name not in PROVIDERS:
            return {"ok": False, "error": t("unknown_provider", name=name, supported=", ".join(sorted(PROVIDERS)))}
        text = self.config_path.read_text(encoding="utf-8")
        if re.search(r"^provider:.*$", text, flags=re.M):
            text = re.sub(r"^provider:.*$", f"provider: {name}", text, count=1, flags=re.M)
        else:
            text = f"provider: {name}\n" + text
        return self.save_config(text)

    def set_app_adapter(self, body: dict) -> dict:
        """Remember which network adapter belongs to the VPN app (matched by its exact name)."""
        name = str(body.get("name", "")).strip()
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        data["app_vpn"] = {"adapter_match": [name] if name else []}
        return self.save_config(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def manager_log(self, tail: int) -> str:
        path = (self.mgr.cfg.state_dir / self.mgr.cfg.log.file) if self.mgr else self.base_dir / ".hmaproxy/logs/hmaproxy.log"
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        return redact("\n".join(lines))


# ======================================================================
class Handler(BaseHTTPRequestHandler):
    app: WebApp  # set by make_server
    server_version = "hmaproxy"

    def log_message(self, fmt: str, *args) -> None:  # route access log to our logger at DEBUG
        get_logger("web").debug(fmt, *args)

    # -------------------------------------------------------------- guards
    def _authorized(self) -> bool:
        pw = self.app.web.password
        if not pw:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:  # noqa: BLE001
                return False
            return hmac.compare_digest(given.encode(), pw.encode())
        return False

    def _host_ok(self) -> bool:
        bind = self.app.web.bind
        if bind not in ("127.0.0.1", "localhost", "::1"):
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _guard(self, mutating: bool) -> bool:
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, {"error": "bad Host header"})
            return False
        if not self._authorized():
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="hmaproxy"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if mutating and self.headers.get("X-HMA-Request") != "1":
            self._send(HTTPStatus.FORBIDDEN, {"error": "missing X-HMA-Request header"})
            return False
        return True

    # -------------------------------------------------------------- io
    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        if content_type == "application/json":
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HmaProxyError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HmaProxyError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise HmaProxyError("JSON body must be an object")
        return data

    def _run(self, fn) -> None:
        try:
            result = fn()
        except NotFoundError as exc:
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
        except HmaProxyError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            get_logger("web").exception("request failed")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": redact(str(exc))})
        else:
            if isinstance(result, tuple):
                self._send(HTTPStatus.OK, result[0], content_type=result[1])
            else:
                self._send(HTTPStatus.OK, result)

    # -------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802
        if not self._guard(mutating=False):
            return
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path
        app = self.app
        if path in ("/", "/index.html"):
            html = resources.files("hmaproxy").joinpath("static/index.html").read_bytes()
            return self._send(HTTPStatus.OK, html, content_type="text/html")
        if path == "/api/status":
            return self._run(app.status)
        if path == "/api/config":
            return self._run(lambda: {"yaml": app.config_path.read_text(encoding="utf-8"),
                                      "path": str(app.config_path), "error": app.config_error})
        if path == "/api/export":
            def do_export():
                mgr = app.require_mgr()
                fmt = q.get("format", "http")
                if fmt not in FORMATS:
                    raise HmaProxyError(f"format: {', '.join(FORMATS)}")
                if fmt == "socks5" and not mgr.cfg.socks5.enabled:
                    raise HmaProxyError(t("socks_disabled"))
                text = export_proxies(mgr.cfg, [r for r in mgr.rows() if r["created"]], fmt,
                                      host=q.get("host") or None, include_auth=q.get("auth", "1") == "1",
                                      only_healthy=q.get("only_healthy") == "1")
                return (text, "application/json" if fmt == "json" else "text/plain")
            return self._run(do_export)
        m = re.fullmatch(r"/api/proxies/([a-z0-9_-]+)/logs", path)
        if m:
            tail = max(10, min(int(q.get("tail", "200")), 5000))
            return self._run(lambda: (app.require_mgr().logs(m.group(1), tail=tail, socks=q.get("socks") == "1"),
                                      "text/plain"))
        if path == "/api/logs":
            tail = max(10, min(int(q.get("tail", "300")), 5000))
            return self._run(lambda: (app.manager_log(tail), "text/plain"))
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard(mutating=True):
            return
        path = urlparse(self.path).path
        app = self.app
        try:
            body = self._body()
        except HmaProxyError as exc:
            return self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        if path == "/api/create":
            return self._run(lambda: app.action("create", "all", body))
        m = re.fullmatch(r"/api/proxies/([a-z0-9_-]+)/(start|stop|restart|remove|check)", path)
        if m:
            return self._run(lambda: app.action(m.group(2), m.group(1), body))
        if path == "/api/config":
            return self._run(lambda: app.save_config(str(body.get("yaml", ""))))
        if path == "/api/config/proxies":
            return self._run(lambda: app.add_proxy(body))
        if path == "/api/app-adapter":
            return self._run(lambda: app.set_app_adapter(body))
        if path == "/api/provider":
            return self._run(lambda: app.set_provider(body))
        if path == "/api/credentials":
            return self._run(lambda: app.save_credentials(body))
        if path == "/api/ovpn":
            return self._run(lambda: app.upload_ovpn(body))
        if path == "/api/reload":
            return self._run(lambda: (app.reload(), {"ok": app.config_error is None, "error": app.config_error})[1])
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._guard(mutating=True):
            return
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/config/proxies/([a-z0-9_-]+)", path)
        if m:
            return self._run(lambda: self.app.delete_proxy_from_config(m.group(1)))
        m = re.fullmatch(r"/api/ovpn/([^/]+)", path)
        if m:
            from urllib.parse import unquote

            return self._run(lambda: self.app.delete_ovpn(unquote(m.group(1))))
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})


def make_server(app: WebApp, bind: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((bind or app.web.bind, app.web.port if port is None else port), handler)
    server.daemon_threads = True
    return server


def serve(config_path: Path, bind: str | None = None, port: int | None = None, open_browser: bool | None = None) -> int:
    app = WebApp(config_path)
    server = make_server(app, bind, port)
    host, real_port = server.server_address[:2]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{real_port}/"
    if sys.stdout is not None:  # None under pythonw.exe (autostart without a console)
        print(f"HMA Proxy Manager {__version__} - web UI: {url}  (Ctrl+C để dừng / to stop)", flush=True)
    app.log.info("web UI listening on %s", url)
    if app.web.bind not in ("127.0.0.1", "localhost", "::1") and not app.web.password:
        app.log.warning("web UI is reachable from other machines without a password (web.password)")
    if open_browser if open_browser is not None else app.web.open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()
    return 0
