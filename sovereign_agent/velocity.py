"""
velocity.py — SDLC velocity and quality dashboard.

Reads logs/velocity.jsonl (one JSON record per task, written by work.py)
and prints a human-readable report showing:
  - Daily throughput (tasks done / failed / total)
  - Average retries per completed task
  - Overall first-attempt success rate
  - Top recurring error types (these are the ones slowing you down)
  - Task-level detail for any session with failures

Usage:
    python velocity.py --project ~/Code/astro_flux
    python velocity.py --project ~/Code/astro_flux --days 7
"""

import os
import re
import sys
import json
import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta

BOLD  = "\033[1m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RED   = "\033[91m"
DIM   = "\033[2m"
CYAN  = "\033[96m"
RESET = "\033[0m"

BAR_WIDTH = 28


def load_records(project_root: str) -> list[dict]:
    path = os.path.join(project_root, "logs", "velocity.jsonl")
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def bar(value: float, max_value: float, width: int = BAR_WIDTH) -> str:
    if max_value == 0:
        return " " * width
    filled = round((value / max_value) * width)
    return "█" * filled + "░" * (width - filled)


def rate_color(rate: float) -> str:
    """Green ≥80%, yellow ≥50%, red below."""
    if rate >= 0.8:
        return GREEN
    if rate >= 0.5:
        return YELLOW
    return RED


def report(records: list[dict], days: int):
    if not records:
        print(f"{YELLOW}No velocity data yet. Run work.py to generate it.{RESET}")
        return

    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    records = [r for r in records if r.get("date", "") >= cutoff]

    if not records:
        print(f"{YELLOW}No records in the last {days} day(s).{RESET}")
        return

    # ── Aggregate by date ──────────────────────────────────────────────────────
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_date[r["date"]].append(r)

    all_done   = [r for r in records if r["outcome"] == "done"]
    all_failed = [r for r in records if r["outcome"] == "failed"]
    total      = len(records)

    first_pass = sum(1 for r in all_done if r.get("attempts", 99) == 1)
    avg_retries = (
        sum(r.get("attempts", 1) for r in all_done) / len(all_done)
        if all_done else 0
    )

    # ── Error type frequency ───────────────────────────────────────────────────
    error_counter: Counter = Counter()
    for r in records:
        if r["outcome"] == "failed" or r.get("attempts", 1) > 1:
            for e in r.get("error_types", []):
                error_counter[e] += 1

    # ── Print header ───────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'━'*56}{RESET}")
    print(f"{BOLD}  📊  AstroFlux SDLC Velocity — last {days} day(s){RESET}")
    print(f"{BOLD}{'━'*56}{RESET}\n")

    # ── Daily table ────────────────────────────────────────────────────────────
    max_daily = max(len(v) for v in by_date.values())
    print(f"  {BOLD}{'Date':<14}{'Done':>6}{'Failed':>8}{'Total':>8}  Success   Avg retries{RESET}")
    print(f"  {'─'*54}")
    for d in sorted(by_date.keys()):
        day_recs  = by_date[d]
        done      = sum(1 for r in day_recs if r["outcome"] == "done")
        failed    = sum(1 for r in day_recs if r["outcome"] == "failed")
        tot       = len(day_recs)
        sr        = done / tot if tot else 0
        day_done  = [r for r in day_recs if r["outcome"] == "done"]
        day_avg   = (sum(r.get("attempts",1) for r in day_done) / len(day_done)) if day_done else 0
        sc        = rate_color(sr)
        print(
            f"  {d:<14}{done:>6}{RED if failed else DIM}{failed:>8}{RESET}"
            f"{DIM}{tot:>8}{RESET}  "
            f"{sc}{sr*100:>5.0f}%{RESET}   "
            f"{CYAN}{day_avg:>5.1f}x{RESET}"
        )

    # ── Summary ────────────────────────────────────────────────────────────────
    overall_sr = len(all_done) / total if total else 0
    print(f"\n  {BOLD}Overall ({total} tasks){RESET}")
    sc = rate_color(overall_sr)
    print(f"    Success rate   {sc}{overall_sr*100:.0f}%{RESET}  "
          f"({len(all_done)} done, {len(all_failed)} failed)")
    print(f"    First-attempt  {GREEN if first_pass/max(len(all_done),1) >= 0.5 else YELLOW}"
          f"{first_pass}/{len(all_done)}{RESET} tasks passed on attempt 1")
    print(f"    Avg retries    {CYAN}{avg_retries:.1f}x{RESET} per completed task")
    print(f"    Time spent     {DIM}{sum(r.get('duration_s',0) for r in records)/60:.0f} min{RESET}")

    # ── Top error types ────────────────────────────────────────────────────────
    if error_counter:
        print(f"\n  {BOLD}Top error types (retries + failures){RESET}")
        top = error_counter.most_common(10)
        max_count = top[0][1]
        for etype, count in top:
            b = bar(count, max_count, 20)
            print(f"    {YELLOW}{etype:<40}{RESET} {RED}{count:>3}x{RESET}  {DIM}{b}{RESET}")
        print(f"\n  {DIM}Tip: the top error types are the highest-ROI targets for .roorules{RESET}")
    else:
        print(f"\n  {GREEN}No recurring errors — clean run!{RESET}")

    # ── Failed tasks ───────────────────────────────────────────────────────────
    if all_failed:
        print(f"\n  {BOLD}{RED}Failed tasks{RESET}")
        for r in all_failed:
            etypes = ", ".join(r.get("error_types", [])[:5]) or "unknown"
            print(f"    {RED}✗{RESET} [{r['date']}] {r['task'][:65]}")
            print(f"      {DIM}errors: {etypes}{RESET}")

    print(f"\n{BOLD}{'━'*56}{RESET}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None, help="Path to project folder")
    parser.add_argument("--days", type=int, default=7, help="How many days to include (default: 7)")
    args = parser.parse_args()

    project_root = os.path.abspath(args.project) if args.project else os.getcwd()
    if not os.path.isdir(project_root):
        print(f"⚠  Not found: {project_root}")
        sys.exit(1)

    records = load_records(project_root)
    report(records, args.days)


if __name__ == "__main__":
    main()
