"""Linux backend: cgroup v2 + systemd user manager (no root needed).

Steam runs inside a delegated systemd scope split into three cgroups:
  core   - the Steam client itself
  web    - steamwebhelper (the Chromium UI, by far the biggest RAM hog)
  games  - anything started by Steam's game launcher ("reaper")
"""

import os
import shutil
import signal
import subprocess
import sys
import time

from .common import PROFILES, human, log, run_governor

CGROOT = "/sys/fs/cgroup"
SCOPE_PREFIX = "steamlite"


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError:
        return False


def own_cgroup():
    for line in read("/proc/self/cgroup").splitlines():
        if line.startswith("0::"):
            return CGROOT + line[3:]
    sys.exit("steamlite: cgroup v2 not found")


def proc_info(pid):
    """(comm, ppid) or None if the process is gone."""
    stat = read(f"/proc/{pid}/stat")
    if not stat:
        return None
    # comm may contain spaces/parens: take what is between first '(' and last ')'
    comm = stat[stat.index("(") + 1:stat.rindex(")")]
    ppid = int(stat[stat.rindex(")") + 2:].split()[1])
    return comm, ppid


def is_webhelper(pid):
    """comm can lie (wrappers, interpreters); check exe and argv[0] too."""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = ""
    argv0 = read(f"/proc/{pid}/cmdline").split("\0")[0]
    return any(os.path.basename(x).startswith("steamwebhelper") for x in (exe, argv0))


def cgroup_pids(cg):
    return [int(p) for p in read(f"{cg}/cgroup.procs").split()]


def mem_current(cg):
    cur = int(read(f"{cg}/memory.current", "0") or 0)
    swap = int(read(f"{cg}/memory.swap.current", "0") or 0)
    return cur, swap


def reclaim(cg, amount):
    # Kernel reclaims what it can; EAGAIN just means "not all of it".
    # Prefer swapping anon pages when the kernel supports the swappiness key.
    if not write(f"{cg}/memory.reclaim", f"{amount} swappiness=200"):
        write(f"{cg}/memory.reclaim", amount)


def status_line(scope):
    parts = []
    total = 0
    for name in ("core", "web", "games"):
        cur, swap = mem_current(f"{scope}/{name}")
        if name != "games":
            total += cur
        parts.append(f"{name}: {human(cur)} (+{human(swap)} swap)")
    return f"steam RAM {human(total)} | " + " | ".join(parts)


def find_scopes():
    found = []
    for root, dirs, _ in os.walk(f"{CGROOT}/user.slice"):
        for d in dirs:
            if d.startswith(SCOPE_PREFIX) and d.endswith(".scope"):
                found.append(os.path.join(root, d))
        # don't descend into steamlite scopes themselves
        dirs[:] = [d for d in dirs if not d.startswith(SCOPE_PREFIX)]
    return found


class Governor:
    def __init__(self, scope, args):
        self.scope = scope
        self.args = args
        self.core = f"{scope}/core"
        self.web = f"{scope}/web"
        self.games = f"{scope}/games"

    def setup(self):
        for cg in (self.core, self.web, self.games):
            os.makedirs(cg, exist_ok=True)
        # A cgroup with children can't hold processes: move ourselves to core
        # before enabling the memory controller for the subtree.
        if not write(f"{self.core}/cgroup.procs", os.getpid()):
            sys.exit("steamlite: could not move into core cgroup")
        if not write(f"{self.scope}/cgroup.subtree_control", "+memory"):
            sys.exit("steamlite: could not enable memory controller "
                     "(is the scope delegated?)")
        self.apply_idle()

    def is_game(self, pid, cache):
        """True if pid descends from Steam's 'reaper' game launcher."""
        for _ in range(64):
            if pid <= 1:
                return False
            info = cache.get(pid) or proc_info(pid)
            if info is None:
                return False
            cache[pid] = info
            if info[0] == "reaper":
                return True
            pid = info[1]
        return False

    # -- governor interface -------------------------------------------

    def alive(self):
        return any(p != os.getpid()
                   for cg in (self.core, self.web, self.games)
                   for p in cgroup_pids(cg))

    def sort(self):
        cache = {}
        for pid in cgroup_pids(self.core):
            if pid == os.getpid():
                continue
            info = proc_info(pid)
            if info is None:
                continue
            cache[pid] = info
            if info[0] == "steamwebhelper" or is_webhelper(pid):
                write(f"{self.web}/cgroup.procs", pid)
            elif self.is_game(pid, cache):
                write(f"{self.games}/cgroup.procs", pid)

    def is_gaming(self):
        return bool(cgroup_pids(self.games))

    def apply_idle(self):
        write(f"{self.web}/memory.high", self.args.ui_limit)
        write(f"{self.core}/memory.high", "max")
        write(f"{self.web}/cgroup.freeze", 0)

    def apply_gaming(self):
        write(f"{self.web}/memory.high", self.args.game_web_limit)
        write(f"{self.core}/memory.high", self.args.game_core_limit)
        self.squeeze()
        if self.args.freeze:
            write(f"{self.web}/cgroup.freeze", 1)

    def squeeze(self):
        reclaim(self.web, mem_current(self.web)[0])
        # Memory charged before a process was sorted stays in `core`
        # (cgroup v2 doesn't move charges), so reclaim there too by default.
        if self.args.game_core_limit != "max" or not self.args.no_reclaim_core:
            reclaim(self.core, mem_current(self.core)[0])

    def steam_mem(self):
        return sum(mem_current(cg)[0] for cg in (self.core, self.web))

    def status_line(self):
        return status_line(self.scope)

    def cleanup(self):
        write(f"{self.web}/cgroup.freeze", 0)
        write(f"{self.web}/memory.high", "max")
        write(f"{self.core}/memory.high", "max")


# ---------------------------------------------------------------- commands

def inner(args):
    """Runs inside the delegated scope: start Steam and govern it."""
    gov = Governor(own_cgroup(), args)
    gov.setup()

    cmd = [shutil.which(args.steam or "steam") or "steam"]
    cmd += PROFILES[args.profile] + args.steam_args
    log("starting: " + " ".join(cmd))
    child = subprocess.Popen(cmd)

    def on_signal(signum, frame):
        # Unfreeze first, or Steam can't shut its UI down; then keep governing
        # until the scope is empty.
        gov.cleanup()
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    run_governor(gov, args)


def launch(args):
    if not shutil.which("systemd-run"):
        sys.exit("steamlite: systemd-run not found")
    if find_scopes():
        sys.exit("steamlite: already running (see `steamlite status`)")
    unit = f"{SCOPE_PREFIX}-{int(time.time())}"
    entry = os.path.realpath(sys.argv[0])
    inner_cmd = [sys.executable, entry, "_inner"] + sys.argv[2:]
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
           f"--unit={unit}", "-p", "Delegate=yes", "--"] + inner_cmd
    os.execvp(cmd[0], cmd)


def status(args):
    scopes = find_scopes()
    if not scopes:
        print("steamlite is not running")
        return
    for scope in scopes:
        print(os.path.basename(scope))
        print("  " + status_line(scope))
        frozen = read(f"{scope}/web/cgroup.freeze") == "1"
        print(f"  web UI limit: {read(f'{scope}/web/memory.high')}"
              f"{'  [FROZEN]' if frozen else ''}")


def squeeze(args):
    for scope in find_scopes():
        for name in ("web", "core"):
            cg = f"{scope}/{name}"
            before = mem_current(cg)[0]
            reclaim(cg, before)
            print(f"{name}: {human(before)} -> {human(mem_current(cg)[0])}")


def thaw(args):
    for scope in find_scopes():
        write(f"{scope}/web/cgroup.freeze", 0)
        for name in ("web", "core"):
            write(f"{scope}/{name}/memory.high", "max")
        print(f"{os.path.basename(scope)}: unfrozen, limits removed")
