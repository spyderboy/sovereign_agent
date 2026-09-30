"""Routing: which tasks a small model should not attempt at all.

Opt-in via .sovereign_config.json (kickoff.py writes it for new projects):

    "routing": {
        "strong_min_params_b": 25,          # models below this count as small
        "cold_start_tasks_per_layer": 2,    # a layer's first N tasks go to a strong model
        "route_after_failures": 2,          # after N small-model failures, stop retrying small
        "park_after_strong_failures": 3     # after N strong-model failures, park for a human
    }

Strong runs also try tasks with the fewest strong failures first, so a pass
isn't spent re-failing the same hard tasks while untried ones starve
(2026-09-30: xmeasure's report layer was never attempted in five cycles).
Strong failures are counted in logs/strong_failures.json — not taken from
history, so a kit fix can reset them (`xrun.py reset-parking`).

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

from sov.telemetry import task_sha


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
        if r.get("task_sha"):
            # An edited task (a kit fix) starts with a clean record; its first
            # 100 chars often don't change (2026-09-30: compute helpers stayed
            # deferred for failures the task-text fix had already removed).
            if r["task_sha"] != task_sha(task_text):
                continue
        elif not stored or not task_text.startswith(stored[:100]):
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


STRIKES = os.path.join("logs", "strong_failures.json")
PARKED = os.path.join("logs", "parked.md")


def _key(task_text: str) -> str:
    return task_text.strip()[:100]


def load_strikes(project_root: str) -> dict[str, int]:
    try:
        return json.load(open(os.path.join(project_root, STRIKES)))
    except (OSError, ValueError):
        return {}


def record_strong_outcome(project_root: str, task_text: str, passed: bool) -> int:
    """Count a strong-model failure (or clear the count on a pass); returns the count."""
    strikes = load_strikes(project_root)
    k = _key(task_text)
    if passed:
        strikes.pop(k, None)
    else:
        strikes[k] = strikes.get(k, 0) + 1
    path = os.path.join(project_root, STRIKES)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(strikes, f, indent=1)
    os.replace(tmp, path)
    return strikes.get(k, 0)


INFLIGHT = os.path.join("logs", "strong_inflight.json")


def begin_strong_attempt(project_root: str, task_text: str) -> None:
    """Count the strike up front (so a pass-cap kill still counts) and mark
    the task in flight, so a deliberate stop can take it back."""
    record_strong_outcome(project_root, task_text, False)
    try:
        with open(os.path.join(project_root, INFLIGHT), "w") as f:
            json.dump({"task": _key(task_text)}, f)
    except OSError:
        pass


def end_strong_attempt(project_root: str, task_text: str, passed: bool) -> None:
    try:
        os.remove(os.path.join(project_root, INFLIGHT))
    except OSError:
        pass
    if passed:
        record_strong_outcome(project_root, task_text, True)


def forgive_inflight(project_root: str) -> str | None:
    """A human stopped the run (xrun stop): the in-flight attempt was not a
    failure. 2026-09-30: two stops to fix the kit left compute helpers with
    strikes they never earned. Returns the forgiven task key, if any."""
    path = os.path.join(project_root, INFLIGHT)
    try:
        key = json.load(open(path)).get("task")
        os.remove(path)
    except (OSError, ValueError):
        return None
    strikes = load_strikes(project_root)
    if strikes.get(key, 0) > 0:
        strikes[key] -= 1
        if not strikes[key]:
            del strikes[key]
        with open(os.path.join(project_root, STRIKES), "w") as f:
            json.dump(strikes, f, indent=1)
    return key


def order_for_strong(indexed_tasks: list, strikes: dict[str, int], task_text_fn) -> list:
    """Fewest strong failures first; ROADMAP order kept within each count."""
    return sorted(indexed_tasks, key=lambda it: strikes.get(_key(task_text_fn(it[1])), 0))


def _note_parked(project_root: str, task_text: str, count: int) -> None:
    path = os.path.join(project_root, PARKED)
    try:
        existing = open(path).read()
    except OSError:
        existing = "# Parked tasks\n\nThe strong model failed these repeatedly. Usually a gap in the task, its test, or an earlier merge — not model difficulty. Fix, then `xrun.py reset-parking <project>`.\n\n"
    line = f"- {_key(task_text)}"
    if line not in existing:
        with open(path, "w") as f:
            f.write(existing + f"{line} (strong failures: {count})\n")


def defer_reason(task_text: str, policy: dict, run_max_params: float,
                 roadmap_text: str, velocity_lines: list[str],
                 params: dict[str, float], strikes: dict[str, int] | None = None) -> str | None:
    """Why this run should leave the task alone, or None."""
    if not policy:
        return None
    strong_min = float(policy.get("strong_min_params_b", 25))
    if run_max_params >= strong_min:
        n_park = int(policy.get("park_after_strong_failures", 0))
        count = (strikes or {}).get(_key(task_text), 0)
        if n_park and count >= n_park:
            return f"parked: the strong model failed this {count}× — needs a human (logs/parked.md)"
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
    strikes = load_strikes(project_root)
    why = defer_reason(task_text, policy, run_max, roadmap, velocity, params, strikes)
    if why and why.startswith("parked"):
        _note_parked(project_root, task_text, strikes.get(_key(task_text), 0))
    return why


def is_strong_run(project_root: str, run_models: list[str], params: dict[str, float]) -> bool:
    policy = load_policy(project_root)
    if not policy:
        return False
    run_max = max((params.get(m, _guess_params(m)) for m in run_models), default=0.0)
    return run_max >= float(policy.get("strong_min_params_b", 25))
