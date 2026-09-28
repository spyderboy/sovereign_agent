"""Per-task time budgets derived from velocity history, and the timing report.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
import json
import os
import time
from sov.config import (
    BUDGET_CEILING_S,
    BUDGET_FLOOR_S,
    BUDGET_MULTIPLIER,
    BUDGET_SAMPLES,
)
from sov.telemetry import VELOCITY_LOG
from sov.ui import BOLD, RESET


# ─── Time budget helpers ─────────────────────────────────────────────────────

def compute_task_budget(project_root: str,
                        multiplier: float = BUDGET_MULTIPLIER,
                        ceiling_s: float = BUDGET_CEILING_S) -> float:
    """Return the time budget in seconds for the next task.

    Uses the rolling average of recently completed tasks × multiplier,
    capped at ceiling_s. Falls back to ceiling_s until there's enough history.

    First pass : multiplier=1.7, ceiling_s=600  (≈10 min when avg is 6 min)
    Retry pass : multiplier=3.4, ceiling_s=1200 (≈20 min when avg is 6 min)
    """
    vel_path = os.path.join(project_root, VELOCITY_LOG)
    if not os.path.exists(vel_path):
        return ceiling_s
    durations: list[float] = []
    with open(vel_path) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("outcome") == "done":
                    durations.append(float(r["duration_s"]))
            except Exception:
                pass
    if len(durations) < 5:
        return ceiling_s   # not enough history yet — be generous
    recent = durations[-BUDGET_SAMPLES:]
    avg = sum(recent) / len(recent)
    return max(BUDGET_FLOOR_S, min(avg * multiplier, ceiling_s))


def print_timing_report(project_root: str, session_start: float) -> None:
    """Print a timing summary from velocity.jsonl."""
    vel_path = os.path.join(project_root, VELOCITY_LOG)
    if not os.path.exists(vel_path):
        return
    records: list[dict] = []
    with open(vel_path) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    if not records:
        return

    done    = [r for r in records if r.get("outcome") == "done"]
    skipped = [r for r in records if r.get("outcome") in ("skipped", "budget", "failed")]

    print(f"\n{BOLD}{'━'*56}{RESET}")
    print(f"{BOLD}  ⏱  Timing Report{RESET}")
    if done:
        durations = sorted(r["duration_s"] for r in done)
        avg  = sum(durations) / len(durations)
        p50  = durations[len(durations) // 2]
        p90  = durations[max(0, int(len(durations) * 0.9) - 1)]
        budget = compute_task_budget(project_root)
        print(f"  Completed       : {len(done)} tasks")
        print(f"  Avg time/task   : {avg:.0f}s  ({avg/60:.1f} min)")
        print(f"  Median (p50)    : {p50:.0f}s")
        print(f"  p90             : {p90:.0f}s")
        print(f"  Fastest         : {durations[0]:.0f}s")
        print(f"  Slowest         : {durations[-1]:.0f}s")
        print(f"  Current budget  : {budget:.0f}s ({budget/60:.1f} min)  "
              f"[avg × {BUDGET_MULTIPLIER}]")
    if skipped:
        budget_hits = sum(1 for r in records if r.get("outcome") == "budget")
        print(f"  Skipped/failed  : {len(skipped)}"
              + (f"  ({budget_hits} by time budget)" if budget_hits else ""))
    session_mins = (time.time() - session_start) / 60
    print(f"  Session time    : {session_mins:.1f} min")
    print(f"{BOLD}{'━'*56}{RESET}\n")
