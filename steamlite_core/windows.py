"""Windows backend: Win32 working-set / priority APIs via ctypes (no admin needed).

There are no cgroups on Windows, so each Steam process is handled directly:
  - working set trimmed (pages go to the standby list / pagefile)
  - hard working-set cap on steamwebhelper.exe
  - memory priority VERY_LOW, IDLE CPU priority and EcoQoS while gaming
  - optional NtSuspendProcess on steamwebhelper.exe (--freeze)
A game is detected through Steam's RunningAppID registry value, falling back to
"a process launched by steam.exe that isn't part of Steam".
"""

import ctypes
import ctypes.wintypes as wt
import shutil
import signal
import subprocess
import sys
import time
import winreg

from .common import PROFILES, human, log, run_governor

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_VM_READ = 0x0010
ACCESS = (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA |
          PROCESS_SET_INFORMATION | PROCESS_SUSPEND_RESUME | PROCESS_VM_READ)

TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value

QUOTA_LIMITS_HARDWS_MIN_DISABLE = 0x2
QUOTA_LIMITS_HARDWS_MAX_ENABLE = 0x4
QUOTA_LIMITS_HARDWS_MAX_DISABLE = 0x8
DEFAULT_WS_MIN, DEFAULT_WS_MAX = 204800, 1413120  # Windows defaults
TRIM = ctypes.c_size_t(-1).value

ProcessMemoryPriority = 0
ProcessPowerThrottling = 4
MEMORY_PRIORITY_VERY_LOW = 1
MEMORY_PRIORITY_NORMAL = 5
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
IDLE_PRIORITY_CLASS = 0x40
NORMAL_PRIORITY_CLASS = 0x20

STEAM_REG = r"Software\Valve\Steam"
CLIENT = "steam.exe"
WEB = "steamwebhelper.exe"
# steam.exe children that are not games
STEAM_NAMES = {CLIENT, WEB, "steamservice.exe", "steamerrorreporter.exe",
               "steamerrorreporter64.exe", "gameoverlayui.exe",
               "gameoverlayui64.exe", "werfault.exe", "conhost.exe"}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
            "PrivateUsage")]


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG),
                ("StateMask", wt.ULONG)]


def _sig(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, argtypes


_sig(k32.CreateToolhelp32Snapshot, wt.HANDLE, wt.DWORD, wt.DWORD)
_sig(k32.Process32FirstW, wt.BOOL, wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_sig(k32.Process32NextW, wt.BOOL, wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_sig(k32.OpenProcess, wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_sig(k32.CloseHandle, wt.BOOL, wt.HANDLE)
_sig(k32.K32GetProcessMemoryInfo, wt.BOOL, wt.HANDLE,
     ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wt.DWORD)
_sig(k32.SetProcessWorkingSetSizeEx, wt.BOOL, wt.HANDLE,
     ctypes.c_size_t, ctypes.c_size_t, wt.DWORD)
_sig(k32.SetPriorityClass, wt.BOOL, wt.HANDLE, wt.DWORD)
_sig(ntdll.NtSuspendProcess, wt.LONG, wt.HANDLE)
_sig(ntdll.NtResumeProcess, wt.LONG, wt.HANDLE)
_set_info = getattr(k32, "SetProcessInformation", None)  # Windows 8+
if _set_info:
    _sig(_set_info, wt.BOOL, wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)


# ---------------------------------------------------------------- win32 helpers

def snapshot():
    """[(pid, ppid, lowercase exe name)] for every process."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    procs = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            procs.append((entry.th32ProcessID, entry.th32ParentProcessID,
                          entry.szExeFile.lower()))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return procs


class Proc:
    """Context manager around an OpenProcess handle; methods ignore failures
    (processes come and go, and some, like steamservice.exe, run as SYSTEM)."""

    def __init__(self, pid):
        self.h = k32.OpenProcess(ACCESS, False, pid)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.h:
            k32.CloseHandle(self.h)

    def memory(self):
        """(working set, private bytes)"""
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(pmc)
        if self.h and k32.K32GetProcessMemoryInfo(self.h, ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize, pmc.PrivateUsage
        return 0, 0

    def trim(self):
        if self.h:
            k32.SetProcessWorkingSetSizeEx(self.h, TRIM, TRIM, 0)

    def cap(self, limit):
        """Hard working-set cap, or remove it when limit == 'max'."""
        if not self.h:
            return
        if limit == "max":
            k32.SetProcessWorkingSetSizeEx(
                self.h, DEFAULT_WS_MIN, DEFAULT_WS_MAX,
                QUOTA_LIMITS_HARDWS_MIN_DISABLE | QUOTA_LIMITS_HARDWS_MAX_DISABLE)
        else:
            k32.SetProcessWorkingSetSizeEx(
                self.h, DEFAULT_WS_MIN, max(limit, DEFAULT_WS_MAX),
                QUOTA_LIMITS_HARDWS_MIN_DISABLE | QUOTA_LIMITS_HARDWS_MAX_ENABLE)

    def background(self, on):
        """Low memory priority + idle CPU + EcoQoS, or back to normal."""
        if not self.h:
            return
        k32.SetPriorityClass(self.h, IDLE_PRIORITY_CLASS if on else NORMAL_PRIORITY_CLASS)
        if not _set_info:
            return
        prio = wt.ULONG(MEMORY_PRIORITY_VERY_LOW if on else MEMORY_PRIORITY_NORMAL)
        _set_info(self.h, ProcessMemoryPriority, ctypes.byref(prio), ctypes.sizeof(prio))
        # ControlMask=0 hands the decision back to Windows
        eco = PROCESS_POWER_THROTTLING_STATE(
            1, PROCESS_POWER_THROTTLING_EXECUTION_SPEED if on else 0,
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED if on else 0)
        _set_info(self.h, ProcessPowerThrottling, ctypes.byref(eco), ctypes.sizeof(eco))

    def suspend(self):
        return bool(self.h) and ntdll.NtSuspendProcess(self.h) == 0

    def resume(self):
        return bool(self.h) and ntdll.NtResumeProcess(self.h) == 0


def reg_value(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STEAM_REG) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def find_steam_exe(override):
    if override:
        return shutil.which(override) or override
    exe = reg_value("SteamExe")
    if exe:
        return exe.replace("/", "\\")
    return r"C:\Program Files (x86)\Steam\steam.exe"


def steam_pids(procs):
    client = [pid for pid, _, name in procs if name == CLIENT]
    web = [pid for pid, _, name in procs if name == WEB]
    return client, web


def mem_of(pids):
    ws = priv = 0
    for pid in pids:
        with Proc(pid) as p:
            w, pr = p.memory()
        ws, priv = ws + w, priv + pr
    return ws, priv


def status_line(procs):
    client, web = steam_pids(procs)
    cws, cpriv = mem_of(client)
    wws, wpriv = mem_of(web)
    return (f"steam RAM {human(cws + wws)} | "
            f"core: {human(cws)} (private {human(cpriv)}) | "
            f"web x{len(web)}: {human(wws)} (private {human(wpriv)})")


# ---------------------------------------------------------------- governor

class Governor:
    def __init__(self, args):
        self.args = args
        self.procs = []
        self.client, self.web = [], []
        self.mode = "idle"
        self.applied = set()  # pids already configured for the current mode
        self.frozen = set()
        self.seen_steam = False
        self.missing = 0
        self.started = time.monotonic()

    def _configure(self, pid, is_web):
        a = self.args
        gaming = self.mode == "gaming"
        with Proc(pid) as p:
            if is_web:
                p.cap(a.game_web_limit if gaming else a.ui_limit)
                p.background(gaming)
                if gaming:
                    p.trim()
                    if a.freeze and pid not in self.frozen and p.suspend():
                        self.frozen.add(pid)
                elif pid in self.frozen and p.resume():
                    self.frozen.discard(pid)
            else:
                p.cap(a.game_core_limit if gaming else "max")
                p.background(gaming)

    def _reconfigure_all(self):
        self.applied.clear()
        self.sort()

    # -- governor interface -------------------------------------------

    def alive(self):
        self.procs = snapshot()
        self.client, self.web = steam_pids(self.procs)
        if self.client:
            self.seen_steam, self.missing = True, 0
            return True
        self.missing += 1
        if self.seen_steam:
            return self.missing < 3
        return time.monotonic() - self.started < 120  # wait for Steam to start

    def sort(self):
        live = set(self.client) | set(self.web)
        self.applied &= live
        self.frozen &= live
        for pid in self.client:
            if pid not in self.applied:
                self._configure(pid, is_web=False)
                self.applied.add(pid)
        for pid in self.web:
            if pid not in self.applied:
                self._configure(pid, is_web=True)
                self.applied.add(pid)

    def is_gaming(self):
        if reg_value("RunningAppID"):
            return True
        clients = set(self.client)
        return any(ppid in clients and name not in STEAM_NAMES
                   for _, ppid, name in self.procs)

    def apply_idle(self):
        self.mode = "idle"
        self._reconfigure_all()

    def apply_gaming(self):
        self.mode = "gaming"
        self._reconfigure_all()
        self.squeeze()

    def squeeze(self):
        pids = list(self.web)
        if self.args.game_core_limit != "max" or not self.args.no_reclaim_core:
            pids += self.client
        for pid in pids:
            with Proc(pid) as p:
                p.trim()

    def steam_mem(self):
        return mem_of(self.client)[0] + mem_of(self.web)[0]

    def status_line(self):
        return status_line(self.procs)

    def cleanup(self):
        # Hard caps and suspensions outlive us, so always undo them.
        procs = snapshot()
        client, web = steam_pids(procs)
        for pid in client + web:
            with Proc(pid) as p:
                p.cap("max")
                p.background(False)
                if pid in self.frozen:
                    p.resume()
        self.frozen.clear()


# ---------------------------------------------------------------- commands

def launch(args):
    def on_break(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGBREAK, on_break)  # console window closed

    if steam_pids(snapshot())[0]:
        log("Steam is already running: flags not applied, governing it anyway "
            "(close Steam first for the full effect)")
    else:
        cmd = [find_steam_exe(args.steam)] + PROFILES[args.profile] + args.steam_args
        log("starting: " + " ".join(cmd))
        subprocess.Popen(cmd, close_fds=True)
    run_governor(Governor(args), args)


def status(args):
    procs = snapshot()
    if not steam_pids(procs)[0]:
        print("Steam is not running")
        return
    print(status_line(procs))
    app = reg_value("RunningAppID")
    print(f"  running game: {app if app else 'none'}")


def squeeze(args):
    client, web = steam_pids(snapshot())
    for label, pids in (("web", web), ("core", client)):
        before = mem_of(pids)[0]
        for pid in pids:
            with Proc(pid) as p:
                p.trim()
        print(f"{label}: {human(before)} -> {human(mem_of(pids)[0])}")


def thaw(args):
    """Undo everything (e.g. if steamlite was killed while Steam was frozen)."""
    client, web = steam_pids(snapshot())
    for pid in client + web:
        with Proc(pid) as p:
            p.resume()
            p.cap("max")
            p.background(False)
    print(f"restored {len(client) + len(web)} Steam processes")
