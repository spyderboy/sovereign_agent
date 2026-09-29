"""Routing: which tasks a small model should not attempt at all.

Opt-in via .sovereign_config.json (kickoff.py writes it for new projects):

    "routing": {
        "strong_min_params_b": 25,          # models below this count as small
        "cold_start_tasks_per_layer": 2,    # a layer's first N tasks go to a strong model
        "route_after_failures": 2           # after N small-model failures, stop retrying small
    }

WHY (2026-09-28, xmeasure): the fast pass spent three 90-minute passes
retrying store-layer tasks the 14B had already failed, landing one task per
pass, and a new layer's first merges become the examples later tasks copy —
a weak first merge teaches its mistakes downstream. Deferring those tasks on
small-model runs leaves them for the strong pass instead of burning attempts.

A deferred task is simply not attempted in this run (not marked skipped, not
queued for the retry pass). Without a "routing" section nothing changes.
"""
import json
import os
import re


def load_policy(project_root: str) -> dict:
    try:
        cfg = json.load(open(os.path.join(project_root, ".sovereign_config.json")))
    except (OSError, ValueError):
        return {}
    pol = cfg.get("routing")
    return pol if isinstance(pol, dict) else {}


def task_layer(task_text: str) -> str | None:
    """The directory a task's target lives in: `xmeasure/store/ci_io.py` → `xmeasure/store`."""
    m = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", task_text)
    if not m:
        return None
    return os.path.dirname(m.group(1)) or "."


def layer_done_counts(roadmap_text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in roadmap_text.splitlines():
        if line.startswith("- [x]"):
            layer = task_layer(line)
            if layer is not None:
                counts[layer] = counts.get(layer, 0) + 1
    return counts


def small_failures(velocity_lines: list[str], task_text: str, params: dict[str, float],
                   strong_min: float) -> int:
    """Failed/skipped outcomes for this task by models below `strong_min`.
    velocity.jsonl stores the task text truncated, so match on its prefix."""
    n = 0
    for line in velocity_lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        stored = r.get("task") or ""
        if not stored or not task_text.startswith(stored[:100]):
            continue
        if r.get("outcome") == "done":
            continue
        size = params.get(r.get("model") or "", _guess_params(r.get("model") or ""))
        if size < strong_min:
            n += 1
    return n


def _guess_params(model: str) -> float:
    m = re.search(r":(\d+(?:\.\d+)?)b", model)
    return float(m.group(1)) if m else 0.0


def defer_reason(task_text: str, policy: dict, run_max_params: float,
                 roadmap_text: str, velocity_lines: list[str],
                 params: dict[str, float]) -> str | None:
    """Why a small-model run should leave this task alone, or None."""
    if not policy:
        return None
    strong_min = float(policy.get("strong_min_params_b", 25))
    if run_max_params >= strong_min:
        return None                                   # this run IS strong
    n_cold = int(policy.get("cold_start_tasks_per_layer", 0))
    layer = task_layer(task_text)
    if n_cold and layer is not None:
        done = layer_done_counts(roadmap_text).get(layer, 0)
        if done < n_cold:
            return (f"cold start: layer {layer}/ has {done} done task(s), "
                    f"first {n_cold} go to a strong model")
    n_fail = int(policy.get("route_after_failures", 0))
    if n_fail:
        fails = small_failures(velocity_lines, task_text, params, strong_min)
        if fails >= n_fail:
            return f"small models have failed this task {fails}×"
    return None


def check(task_text: str, project_root: str, run_models: list[str],
          params: dict[str, float]) -> str | None:
    """defer_reason() with the inputs read from disk (the worker's checkout)."""
    policy = load_policy(project_root)
    if not policy:
        return None
    run_max = max((params.get(m, _guess_params(m)) for m in run_models), default=0.0)
    try:
        roadmap = open(os.path.join(project_root, "ROADMAP.md"), errors="ignore").read()
    except OSError:
        roadmap = ""
    try:
        velocity = open(os.path.join(project_root, "logs", "velocity.jsonl"),
                        errors="ignore").read().splitlines()
    except OSError:
        velocity = []
    return defer_reason(task_text, policy, run_max, roadmap, velocity, params)
