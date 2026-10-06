"""Shared bits: Steam flag profiles, formatting, logging and the governor loop."""

import os
import sys
import time

PROFILES = {
    # Safe: the UI keeps working normally, just cheaper.
    "safe": [
        "-silent",
        "-nofriendsui",
        "-nochatui",
        "-cef-disable-gpu",
        "-cef-disable-gpu-compositing",
    ],
    # Aggressive: fewer Chromium processes. May be less stable on some versions.
    "aggressive": [
        "-silent",
        "-nofriendsui",
        "-nochatui",
        "-cef-disable-gpu",
        "-cef-disable-gpu-compositing",
        "-cef-single-process",
        "-cef-in-process-gpu",
        "-cef-disable-breakpad",
    ],
}


def parse_size(text):
    """'300M' -> bytes, 'max' -> 'max'."""
    text = str(text).strip()
    if text.lower() in ("max", "none", "0"):
        return "max"
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    if text[-1].upper() in units:
        return int(float(text[:-1]) * units[text[-1].upper()])
    return int(text)


def human(n):
    n = float(n)
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.1f}{unit}"
        n /= 1024


_logfile = None


def log(msg):
    """Print, or append to a log file when there is no console (pythonw.exe)."""
    global _logfile
    line = f"[steamlite {time.strftime('%H:%M:%S')}] {msg}"
    if sys.stdout is not None:
        print(line, flush=True)
        return
    if _logfile is None:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        os.makedirs(os.path.join(base, "steamlite"), exist_ok=True)
        _logfile = open(os.path.join(base, "steamlite", "steamlite.log"), "a")
    _logfile.write(line + "\n")
    _logfile.flush()


def run_governor(gov, args):
    """Platform-independent loop. `gov` implements:

    alive() sort() is_gaming() apply_idle() apply_gaming() squeeze()
    steam_mem() status_line() cleanup()
    """
    gaming = False
    last_squeeze = 0.0
    peak = 0
    try:
        while gov.alive():
            gov.sort()
            now_gaming = gov.is_gaming()
            if now_gaming != gaming:
                gaming = now_gaming
                if gaming:
                    log("game detected -> squeezing Steam")
                    gov.apply_gaming()
                    last_squeeze = time.monotonic()
                else:
                    log("game closed -> relaxing limits")
                    gov.apply_idle()
            elif gaming and time.monotonic() - last_squeeze > args.reclaim_every:
                gov.squeeze()
                last_squeeze = time.monotonic()

            peak = max(peak, gov.steam_mem())
            if args.verbose:
                log(gov.status_line())
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        gov.cleanup()
    log(f"steam exited. peak steam RAM: {human(peak)}")
