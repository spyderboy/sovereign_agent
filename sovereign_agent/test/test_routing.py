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
