#!/usr/bin/env python3
"""steamlite - make Steam use a ridiculously low amount of RAM (Linux and Windows).

Steam is started with lean Chromium (CEF) flags, and while a game is running
its UI (steamwebhelper) and client are squeezed out of RAM: trimmed to
swap/pagefile, capped, deprioritized and optionally frozen. Games are never
limited. When the game closes, everything is relaxed again.

  Linux:   cgroup v2 + systemd user scope (memory.high / memory.reclaim / freeze)
  Windows: working-set trim + hard cap, memory/CPU priority, NtSuspendProcess
"""

import argparse
import sys

from steamlite_core.common import PROFILES, parse_size

if sys.platform == "win32":
    from steamlite_core import windows as backend
elif sys.platform.startswith("linux"):
    from steamlite_core import linux as backend
else:
    sys.exit("steamlite: only Linux and Windows are supported")


def build_parser():
    p = argparse.ArgumentParser(prog="steamlite", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def launch_opts(sp):
        sp.add_argument("--profile", choices=PROFILES, default="safe",
                        help="Steam/CEF flag set (default: safe)")
        sp.add_argument("--ui-limit", type=parse_size, default="max",
                        help="RAM limit for the Steam UI when NOT gaming "
                             "(e.g. 400M; default: max = no limit)")
        sp.add_argument("--game-web-limit", type=parse_size, default=parse_size("32M"),
                        help="RAM limit for the Steam UI while gaming (default 32M)")
        sp.add_argument("--game-core-limit", type=parse_size, default="max",
                        help="RAM limit for the Steam client while gaming "
                             "(default: max; try 150M if you don't need downloads)")
        sp.add_argument("--no-reclaim-core", action="store_true",
                        help="don't page out the Steam client when a game starts")
        sp.add_argument("--freeze", action="store_true",
                        help="freeze the Steam UI while gaming (breaks the Shift+Tab "
                             "overlay, friends notifications, etc.)")
        sp.add_argument("--reclaim-every", type=float, default=30,
                        help="seconds between re-squeezes while gaming (default 30)")
        sp.add_argument("--interval", type=float, default=1.0,
                        help="poll interval in seconds (default 1)")
        sp.add_argument("--steam", default=None,
                        help="Steam executable (default: auto-detect)")
        sp.add_argument("-v", "--verbose", action="store_true")
        sp.add_argument("steam_args", nargs=argparse.REMAINDER,
                        help="extra args passed to Steam (after --)")

    launch_opts(sub.add_parser("launch", help="start Steam under steamlite"))
    if hasattr(backend, "inner"):
        launch_opts(sub.add_parser("_inner"))
    sub.add_parser("status", help="show Steam memory usage")
    sub.add_parser("squeeze", help="push Steam out of RAM right now")
    sub.add_parser("thaw", help="undo freezes/limits (if steamlite was killed)")
    return p


def main():
    args = build_parser().parse_args()
    if getattr(args, "steam_args", None) and args.steam_args[0] == "--":
        args.steam_args = args.steam_args[1:]
    command = "inner" if args.command == "_inner" else args.command
    getattr(backend, command)(args)


if __name__ == "__main__":
    main()
