"""Persistent state shared by the CLI and the monitor (state_dir/state.json).

Holds per-proxy runtime info (last IP, failures, recovery attempts, desired
state) and the server plan chosen in random-selection mode. Writes are atomic
and guarded by an flock so the CLI and the monitor daemon can both update it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_PROXY_STATE: dict[str, Any] = {
    "desired": "running",       # running | stopped (set by start/stop commands)
    "health": "unknown",        # unknown | starting | healthy | unhealthy | failed
    "ip": None,
    "socks_ok": None,
    "last_check": None,
    "last_ok": None,
    "last_error": None,
    "consecutive_failures": 0,
    "restart_attempts": 0,
    "server_switches": 0,
    "tried_servers": [],
    "next_attempt_at": 0.0,
    "started_at": None,         # when the manager last (re)started it
    "ovpn": None,
}


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _flock(fh, exclusive: bool) -> None:
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _funlock(fh) -> None:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _flock(fh, exclusive: bool) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    def _funlock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


# Same-process threads (web server, monitor) also serialise through this lock,
# because Windows file locks are per-process, not per-thread.
_thread_lock = threading.RLock()


def _replace(src: str, dst: Path) -> None:
    # On Windows os.replace fails while another process has the file open; retry briefly.
    for _ in range(50):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(src, dst)


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.lock_path = state_dir / "state.lock"

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data.setdefault("proxies", {})
        data.setdefault("plan", None)
        data.setdefault("real_ip", None)
        return data

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._read()
        self._ensure_dir()
        with _thread_lock, open(self.lock_path, "a+") as lock:
            _flock(lock, exclusive=False)
            try:
                return self._read()
            finally:
                _funlock(lock)

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Read-modify-write under an exclusive lock."""
        self._ensure_dir()
        with _thread_lock, open(self.lock_path, "a+") as lock:
            _flock(lock, exclusive=True)
            try:
                data = self._read()
                yield data
                fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state.", suffix=".json")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(data, fh, indent=2, sort_keys=True)
                    _replace(tmp, self.path)
                except BaseException:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    raise
            finally:
                _funlock(lock)

    # convenience -------------------------------------------------------
    @staticmethod
    def proxy(data: dict[str, Any], proxy_id: str) -> dict[str, Any]:
        entry = data["proxies"].setdefault(proxy_id, {})
        for k, v in DEFAULT_PROXY_STATE.items():
            if k not in entry:
                entry[k] = list(v) if isinstance(v, list) else v
        return entry

    def update_proxy(self, proxy_id: str, **fields: Any) -> dict[str, Any]:
        with self.transaction() as data:
            entry = self.proxy(data, proxy_id)
            entry.update(fields)
            return dict(entry)

    def get_proxy(self, proxy_id: str) -> dict[str, Any]:
        data = self.load()
        entry = dict(DEFAULT_PROXY_STATE)
        entry.update(data["proxies"].get(proxy_id, {}))
        return entry

    def drop_proxy(self, proxy_id: str) -> None:
        with self.transaction() as data:
            data["proxies"].pop(proxy_id, None)
            if data.get("plan"):
                data["plan"] = [e for e in data["plan"] if e.get("id") != proxy_id] or None
