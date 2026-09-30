"""check_task_size: a task that calls functions it never names is oversized
(xmeasure compute_window, 2026-09-30), but a phrase that refers back to a
function the task does name is fine."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from check_task_size import oversized  # noqa: E402

GATE = " — done when: its tests pass; task gate: pytest -q tests/x.py passes"


def flagged(text: str) -> bool:
    return any("does not name" in f.message for f in oversized("- [ ] " + text + GATE))


def test_unnamed_per_item_callee_is_flagged():
    assert flagged("In `x/compute.py`: implement `compute_window(d: Dataset) -> R` — one result per "
                   "METRIC_IDS: call that metric's function from xmeasure.metrics.*")
    assert flagged("In `x/a.py`: implement `f(k: str) -> int` — dispatch to the corresponding handler")


def test_every_call_written_out_is_fine():
    assert not flagged("In `x/flow.py`: implement `flow_metric(id: str, d: Dataset) -> R` — "
                       "'throughput' → `throughput(d.pull_requests, window)`; any other id → raise ValueError")


def test_reference_back_to_a_named_function_is_fine():
    assert not flagged("In lib/ai/term_choke.dart: implement `int chokeTerm(GameState g)` — do not "
                       "recompute the cut set; call the cached function. Import cutVertices from cut_vertices.dart.")
    assert not flagged("In lib/sim/moteMove.dart: implement `bool moteAdvance(Mote m)` — use the free "
                       "functions dist/sub/scale only.")
