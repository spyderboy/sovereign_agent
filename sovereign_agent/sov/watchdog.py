"""Stall watchdog: a heartbeat the task loop refreshes, and a thread that
kills the process when it goes stale (a hung model call must not wedge a run).

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
import os
import time
from sov.ui import RED, RESET


# ─── Stall watchdog ───────────────────────────────────────────────────────────
# The hard task ceiling is checked at the top of each attempt, so a worker that
# never RETURNS from a call never reaches it. On 2026-08-12 two orphaned workers
# deadlocked on the git index and sat for six hours: model idle, ceiling never
# evaluated, nothing written to any log.
#
# This detects the one thing that is unambiguous from outside — no progress at
# all — and exits so the supervisor can relaunch. Exiting is the right recovery
# because the usual cause (a lock held by a dead sibling) clears when the
# process does. It deliberately does NOT enforce the task ceiling: parking does
# not stop a retry, so a ceiling-based exit would re-hang on the same task
# forever, which is worse than the hang it replaced.
_HEARTBEAT = time.time()
_HEARTBEAT_NOTE = "starting"
STALL_LIMIT_S = float(os.environ.get("WATCHDOG_STALL_S", 2700))


def heartbeat(note: str = "") -> None:
    """Record that the worker is still making progress."""
    global _HEARTBEAT, _HEARTBEAT_NOTE
    _HEARTBEAT = time.time()
    if note:
        _HEARTBEAT_NOTE = note


def _start_stall_watchdog() -> None:
    import threading

    def _watch() -> None:
        while True:
            time.sleep(60)
            idle = time.time() - _HEARTBEAT
            if idle > STALL_LIMIT_S:
                print(f"\n{RED}⛔ STALLED — no progress for {idle/60:.0f} min "
                      f"(last: {_HEARTBEAT_NOTE}). Exiting so the supervisor can "
                      f"relaunch.{RESET}\n"
                      f"   If this repeats, check for a second work.py: two "
                      f"workers on one repo deadlock on the git index.",
                      flush=True)
                os._exit(4)

    threading.Thread(target=_watch, daemon=True, name="stall-watchdog").start()
