"""Windows helpers: admin check, locating OpenVPN, virtual adapters (tapctl), job objects.

Everything degrades to harmless no-ops on other platforms so the rest of the
code (and the test-suite) runs anywhere.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import HmaProxyError
from .i18n import t

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

DRIVER_HWIDS = {
    "tap-windows6": "root\\tap0901",
    "wintun": "wintun",
    "ovpn-dco": "ovpn-dco",
}


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def find_openvpn(configured: str = "") -> Path | None:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    found = shutil.which("openvpn")
    if found:
        candidates.append(Path(found))
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "OpenVPN" / "bin" / "openvpn.exe")
    candidates.append(Path(r"C:\Program Files\OpenVPN\bin\openvpn.exe"))
    for c in candidates:
        if c.is_file():
            return c
    return None


def openvpn_version(exe: Path) -> str:
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=10,
                             creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    first = (out.stdout or out.stderr or "").strip().splitlines()
    return first[0] if first else ""


class AdapterManager:
    """Create / list / delete OpenVPN virtual adapters with tapctl.exe (ships with OpenVPN)."""

    def __init__(self, openvpn_exe: Path | None, driver: str):
        self.driver = driver
        self.tapctl = (openvpn_exe.parent / "tapctl.exe") if openvpn_exe else None

    def _run(self, *args: str) -> str:
        if self.tapctl is None or not self.tapctl.is_file():
            raise HmaProxyError(t("tapctl_missing"))
        try:
            res = subprocess.run([str(self.tapctl), *args], capture_output=True, text=True, timeout=60,
                                 creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HmaProxyError(t("tapctl_failed", error=exc)) from exc
        if res.returncode != 0:
            raise HmaProxyError(t("tapctl_failed", error=(res.stderr or res.stdout).strip()))
        return res.stdout

    def list(self) -> dict[str, str]:
        """name -> GUID"""
        out: dict[str, str] = {}
        for line in self._run("list").splitlines():
            line = line.strip()
            if line.startswith("{") and "}" in line:
                guid, _, name = line.partition("}")
                out[name.strip()] = guid + "}"
        return out

    def ensure(self, name: str) -> None:
        if name in self.list():
            return
        self._run("create", "--name", name, "--hwid", DRIVER_HWIDS[self.driver])

    def delete(self, name: str) -> None:
        if name in self.list():
            self._run("delete", name)


class KillOnCloseJob:
    """Windows job object: child processes die automatically if this program exits or crashes,
    so no orphan openvpn.exe keeps a tunnel open."""

    def __init__(self) -> None:
        self.handle = None
        if not IS_WINDOWS:
            return
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):  # noqa: N801
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.handle = job
            self._k32 = k32

    def add(self, proc: subprocess.Popen) -> None:
        if self.handle is None:
            return
        try:
            self._k32.AssignProcessToJobObject(self.handle, int(proc._handle))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best effort
            pass


def kill_pid(pid: int) -> None:
    """Kill a stale openvpn.exe left from a previous run (only if it really is openvpn)."""
    if not pid:
        return
    if IS_WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, creationflags=CREATE_NO_WINDOW).stdout.lower()
            if "openvpn" in out:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10,
                               creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
