#!/usr/bin/env python3
"""
ab_compare.py — head-to-head A/B report for two worker arms on the SAME graph.

Reads each arm's velocity.jsonl (per-task outcome) and task_traces.jsonl
(per-attempt tier detail) and prints a side-by-side table: tasks done,
avg/median/max attempts, escalations, autofix saves, wall time, and the
top error types. Stdlib only — no venv needed.

Default arms match the GalaxicanJS qwen2.5-vs-qwen3 A/B:
    qwen2.5  ->  ~/GalaxicanJS-ab/qwen25   (baseline archived at reset)
    qwen3    ->  ~/Code/GalaxicanJS/logs   (live run)

Usage:
    python ab_compare.py
    python ab_compare.py --arm qwen2.5=~/GalaxicanJS-ab/qwen25 \
                         --arm qwen3=~/Code/GalaxicanJS/logs
"""

import argparse
import json
import os
import statistics
from collections import Counter, defaultdict

DEFAULT_ARMS = [
    ("qwen2.5", "~/GalaxicanJS-ab/qwen25"),
    ("qwen3", "~/Code/GalaxicanJS/logs"),
]


def _load_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def collect(label, logdir):
    logdir = os.path.abspath(os.path.expanduser(logdir))
    vel = _load_jsonl(os.path.join(logdir, "velocity.jsonl"))
    traces = _load_jsonl(os.path.join(logdir, "task_traces.jsonl"))

    done = [r for r in vel if r.get("outcome") == "done"]
    attempts = [r.get("attempts", 0) for r in done if isinstance(r.get("attempts"), (int, float))]
    durations = [r.get("duration_s", 0) for r in done if isinstance(r.get("duration_s"), (int, float))]

    # Per-task tier spread from traces -> escalation = attempts spanning >1 tier.
    tiers_by_task = defaultdict(set)
    autofix = 0
    attempt_rows = [t for t in traces if t.get("record_type") in (None, "attempt") and "tier" in t]
    for t in attempt_rows:
        key = t.get("task_idx", t.get("task"))
        if t.get("tier") is not None:
            tiers_by_task[key].add(t.get("tier"))
        if t.get("autofix_resolved"):
            autofix += 1
    escalations = sum(1 for s in tiers_by_task.values() if len(s) > 1)

    # Which tier closed each task (the winning attempt).
    won_tier = Counter()
    for t in attempt_rows:
        if t.get("validation_passed"):
            won_tier[t.get("tier")] += 1

    err = Counter()
    for r in vel:
        for e in r.get("error_types", []) or []:
            err[e] += 1

    model_mix = Counter(r.get("model", "?") for r in done)

    return {
        "label": label,
        "logdir": logdir,
        "done": len(done),
        "total_tasks": len(vel),
        "avg_attempts": statistics.mean(attempts) if attempts else 0,
        "median_attempts": statistics.median(attempts) if attempts else 0,
        "max_attempts": max(attempts) if attempts else 0,
        "avg_duration": statistics.mean(durations) if durations else 0,
        "total_wall_s": sum(durations),
        "escalations": escalations,
        "autofix_saves": autofix,
        "won_tier": won_tier,
        "top_errors": err.most_common(5),
        "model_mix": model_mix,
    }


def _fmt_hms(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def render(arms):
    labels = [a["label"] for a in arms]
    w = max(22, *(len(l) for l in labels)) + 2

    def row(name, vals):
        cells = "".join(str(v).ljust(w) for v in vals)
        return f"  {name.ljust(22)}{cells}"

    print("\n  A/B — head to head")
    print("  " + "-" * (24 + w * len(arms)))
    print(row("metric", labels))
    print("  " + "-" * (24 + w * len(arms)))
    print(row("tasks done", [f"{a['done']}/{a['total_tasks']}" for a in arms]))
    print(row("avg attempts", [f"{a['avg_attempts']:.2f}" for a in arms]))
    print(row("median attempts", [f"{a['median_attempts']:.1f}" for a in arms]))
    print(row("max attempts", [a["max_attempts"] for a in arms]))
    print(row("escalations", [a["escalations"] for a in arms]))
    print(row("autofix saves", [a["autofix_saves"] for a in arms]))
    print(row("avg sec/task", [f"{a['avg_duration']:.1f}" for a in arms]))
    print(row("total wall time", [_fmt_hms(a["total_wall_s"]) for a in arms]))
    print("  " + "-" * (24 + w * len(arms)))

    for a in arms:
        wt = ", ".join(f"tier{k}:{v}" for k, v in sorted(a["won_tier"].items(), key=lambda x: (x[0] is None, x[0])))
        print(f"\n  [{a['label']}] closed by  {wt or 'n/a'}")
        mm = ", ".join(f"{k} ({v})" for k, v in a["model_mix"].most_common())
        print(f"  [{a['label']}] models     {mm or 'n/a'}")
        te = ", ".join(f"{k}×{v}" for k, v in a["top_errors"])
        print(f"  [{a['label']}] top errors {te or 'none'}")

    if len(arms) == 2 and all(a["done"] for a in arms):
        b, c = arms
        da = c["avg_attempts"] - b["avg_attempts"]
        verdict = "fewer" if da < 0 else "more"
        print(f"\n  → {c['label']} averages {abs(da):.2f} {verdict} attempts/task than {b['label']} "
              f"({c['escalations']} vs {b['escalations']} escalations).")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="append", default=[],
                    help="label=logdir (repeatable). Defaults to the GalaxicanJS A/B.")
    args = ap.parse_args()

    pairs = []
    for spec in args.arm:
        if "=" not in spec:
            ap.error(f"--arm expects label=logdir, got: {spec}")
        label, path = spec.split("=", 1)
        pairs.append((label, path))
    if not pairs:
        pairs = DEFAULT_ARMS

    arms = []
    for label, path in pairs:
        a = collect(label, path)
        if a["total_tasks"] == 0:
            print(f"  [{label}] no velocity.jsonl records yet at {a['logdir']}")
        arms.append(a)

    if any(a["total_tasks"] for a in arms):
        render(arms)


if __name__ == "__main__":
    main()
