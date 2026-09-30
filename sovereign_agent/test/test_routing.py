"""sov/routing.py: which tasks a small-model run leaves for the strong pass."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import routing  # noqa: E402

POLICY = {"strong_min_params_b": 25, "cold_start_tasks_per_layer": 2, "route_after_failures": 2}
PARAMS = {"qwen2.5-coder:14b": 14.0, "qwen3.8:27b": 27.0}
ROADMAP = "\n".join([
    "- [x] In `pkg/store/a.py`: done",
    "- [x] In `pkg/store/b.py`: done",
    "- [x] In `pkg/mappers/c.py`: done",
    "- [ ] In `pkg/mappers/d.py`: open",
])
TASK_NEW_LAYER = "In `pkg/metrics/m.py`: implement m()"
TASK_WARM = "In `pkg/store/e.py`: implement e()"


def vel(task, outcome, model):
    return json.dumps({"task": task[:120], "outcome": outcome, "model": model})


def test_no_policy_changes_nothing():
    assert routing.defer_reason(TASK_NEW_LAYER, {}, 14, ROADMAP, [], PARAMS) is None


def test_strong_run_is_never_deferred():
    assert routing.defer_reason(TASK_NEW_LAYER, POLICY, 27, ROADMAP, [], PARAMS) is None


def test_cold_layer_is_deferred_on_small_run():
    why = routing.defer_reason(TASK_NEW_LAYER, POLICY, 14, ROADMAP, [], PARAMS)
    assert why and "cold start" in why and "pkg/metrics" in why


def test_layer_with_one_done_task_is_still_cold():
    why = routing.defer_reason("In `pkg/mappers/x.py`: y", POLICY, 14, ROADMAP, [], PARAMS)
    assert why and "1 done" in why


def test_warm_layer_runs_on_small_model():
    assert routing.defer_reason(TASK_WARM, POLICY, 14, ROADMAP, [], PARAMS) is None


def test_repeated_small_failures_route_to_strong():
    v = [vel(TASK_WARM, "skipped", "qwen2.5-coder:14b"), vel(TASK_WARM, "failed", "qwen2.5-coder:14b")]
    why = routing.defer_reason(TASK_WARM, POLICY, 14, ROADMAP, v, PARAMS)
    assert why and "2×" in why


def test_strong_model_failures_do_not_count():
    v = [vel(TASK_WARM, "skipped", "qwen3.8:27b"), vel(TASK_WARM, "skipped", "qwen3.8:27b")]
    assert routing.defer_reason(TASK_WARM, POLICY, 14, ROADMAP, v, PARAMS) is None


def test_check_reads_from_disk(tmp_path):
    (tmp_path / ".sovereign_config.json").write_text(json.dumps({"routing": POLICY}))
    (tmp_path / "ROADMAP.md").write_text(ROADMAP)
    assert routing.check(TASK_NEW_LAYER, str(tmp_path), ["qwen2.5-coder:14b"], PARAMS)
    assert routing.check(TASK_NEW_LAYER, str(tmp_path), ["qwen3.8:27b"], PARAMS) is None


# ── parking and strong-pass ordering (2026-09-30) ─────────────────────────────

def test_strikes_count_and_clear(tmp_path):
    t = "In `pkg/x.py`: implement x()"
    assert routing.record_strong_outcome(str(tmp_path), t, False) == 1
    assert routing.record_strong_outcome(str(tmp_path), t, False) == 2
    assert routing.record_strong_outcome(str(tmp_path), t, True) == 0
    assert routing.load_strikes(str(tmp_path)) == {}


def test_parked_after_n_strong_failures_on_strong_run(tmp_path):
    pol = {**POLICY, "park_after_strong_failures": 3}
    t = "In `pkg/store/e.py`: implement e()"
    strikes = {t[:100]: 3}
    why = routing.defer_reason(t, pol, 27, ROADMAP, [], PARAMS, strikes)
    assert why and why.startswith("parked")
    assert routing.defer_reason(t, pol, 27, ROADMAP, [], PARAMS, {t[:100]: 2}) is None


def test_check_writes_parked_md(tmp_path):
    (tmp_path / ".sovereign_config.json").write_text(json.dumps({"routing": {**POLICY, "park_after_strong_failures": 1}}))
    (tmp_path / "ROADMAP.md").write_text(ROADMAP)
    routing.record_strong_outcome(str(tmp_path), TASK_WARM, False)
    assert routing.check(TASK_WARM, str(tmp_path), ["qwen3.8:27b"], PARAMS).startswith("parked")
    assert "pkg/store/e.py" in (tmp_path / "logs" / "parked.md").read_text()


def test_order_for_strong_puts_untried_first_keeping_roadmap_order():
    tasks = [(1, "- [ ] A"), (2, "- [ ] B"), (3, "- [ ] C"), (4, "- [ ] D")]
    strikes = {"B": 2, "A": 1}
    ordered = routing.order_for_strong(tasks, strikes, lambda line: line[6:])
    assert [i for i, _ in ordered] == [3, 4, 1, 2]


# ── 2026-09-30: an edited task starts with a clean small-model record ─────────

def test_edited_task_resets_small_failures(tmp_path):
    import json as _json
    from sov import state, telemetry
    old = "In `pkg/a.py`: implement `f()` — " + "x" * 150 + " import from pkg.metrics"
    new = old.replace("import from pkg.metrics", "import EXACTLY `from pkg.metrics.f import f`")
    state.CURRENT_TASK = old
    for _ in range(2):
        telemetry._append_velocity(str(tmp_path), {"task": old[:120], "outcome": "skipped",
                                                   "model": "qwen2.5-coder:14b"})
    lines = open(tmp_path / "logs" / "velocity.jsonl").read().splitlines()
    assert all("task_sha" in _json.loads(l) for l in lines)
    assert routing.small_failures(lines, old, {}, 25) == 2      # same task: still counted
    assert routing.small_failures(lines, new, {}, 25) == 0      # edited past char 100: clean slate
    legacy = ['{"task": "%s", "outcome": "skipped", "model": "qwen2.5-coder:14b"}' % old[:120]]
    assert routing.small_failures(legacy, new, {}, 25) == 1     # old records: prefix match as before
    state.CURRENT_TASK = ""


def test_deliberate_stop_forgives_the_inflight_strike(tmp_path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    t = "In `pkg/a.py`: implement `f()`"
    routing.begin_strong_attempt(str(tmp_path), t)
    assert routing.load_strikes(str(tmp_path)) == {t: 1}          # a cap kill leaves this
    assert routing.forgive_inflight(str(tmp_path)) == t          # xrun stop takes it back
    assert routing.load_strikes(str(tmp_path)) == {}
    routing.begin_strong_attempt(str(tmp_path), t)
    routing.end_strong_attempt(str(tmp_path), t, passed=False)   # a real failure stays
    assert routing.forgive_inflight(str(tmp_path)) is None
    assert routing.load_strikes(str(tmp_path)) == {t: 1}
