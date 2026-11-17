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


# ---------------------------------------------------------------------------
# Network adapters (used by "app" mode: proxy through a VPN app's adapter)
# ---------------------------------------------------------------------------
def list_adapters() -> list[dict]:
    """Adapters that are UP with an IPv4 address: [{name, description, ip}]."""
    if not IS_WINDOWS:
        return []
    try:
        return _win_adapters()
    except Exception:  # noqa: BLE001 - never break the caller on an API quirk
        return []


def _win_adapters() -> list[dict]:
    from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_ubyte, c_ulong, c_ulonglong, c_void_p, c_wchar_p

    class SOCKET_ADDRESS(Structure):  # noqa: N801
        _fields_ = [("lpSockaddr", c_void_p), ("iSockaddrLength", c_int)]

    class UNICAST(Structure):
        pass

    UNICAST._fields_ = [("Alignment", c_ulonglong), ("Next", POINTER(UNICAST)), ("Address", SOCKET_ADDRESS)]

    class ADDRS(Structure):
        pass

    ADDRS._fields_ = [
        ("Alignment", c_ulonglong), ("Next", POINTER(ADDRS)), ("AdapterName", c_char_p),
        ("FirstUnicastAddress", POINTER(UNICAST)), ("FirstAnycastAddress", c_void_p),
        ("FirstMulticastAddress", c_void_p), ("FirstDnsServerAddress", c_void_p), ("DnsSuffix", c_wchar_p),
        ("Description", c_wchar_p), ("FriendlyName", c_wchar_p), ("PhysicalAddress", c_ubyte * 8),
        ("PhysicalAddressLength", c_ulong), ("Flags", c_ulong), ("Mtu", c_ulong), ("IfType", c_ulong),
        ("OperStatus", c_int),
    ]
    iphlpapi = ctypes.WinDLL("iphlpapi")
    flags = 0x0002 | 0x0004 | 0x0008  # skip anycast, multicast, dns servers
    size = c_ulong(32 * 1024)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        rc = iphlpapi.GetAdaptersAddresses(2, flags, None, buf, byref(size))  # AF_INET
        if rc == 0:
            break
        if rc != 111:  # ERROR_BUFFER_OVERFLOW -> retry with the size Windows asked for
            return []
    else:
        return []
    out = []
    node = ctypes.cast(buf, POINTER(ADDRS))
    while node:
        a = node.contents
        if a.OperStatus == 1:  # IfOperStatusUp
            ua = a.FirstUnicastAddress
            while ua:
                sa = ctypes.cast(ua.contents.Address.lpSockaddr, POINTER(c_ubyte * 8)).contents
                if sa[0] | (sa[1] << 8) == 2:  # AF_INET
                    out.append({"name": a.FriendlyName or "", "description": a.Description or "",
                                "ip": ".".join(str(b) for b in sa[4:8])})
                    break
                ua = ua.contents.Next
        node = a.Next
    return out


def match_adapter(adapters: list[dict], patterns: list[str]) -> dict | None:
    """First adapter whose name or description contains one of the patterns (case-insensitive)."""
    pats = [p.lower() for p in patterns if p]
    for a in adapters:
        text = f"{a.get('name', '')} {a.get('description', '')}".lower()
        if any(p in text for p in pats):
            return a
    return None
