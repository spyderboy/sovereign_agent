"""Same-file regression gates: re-run finished tasks' gates on the same file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import roadmap, state, validation  # noqa: E402

ROAD = "\n".join([
    "- [x] In `pkg/attr.py`: implement `commit_is_ai()` — task gate: pytest -q t.py::test_commit passes",
    "- [x] In `pkg/other.py`: implement `x()` — task gate: pytest -q o.py passes",
    "- [ ] In `pkg/attr.py`: implement `pr_is_ai()` — task gate: pytest -q t.py::test_pr passes",
])
PR_TASK = "In `pkg/attr.py`: implement `pr_is_ai()` — task gate: pytest -q t.py::test_pr passes"


def test_finds_only_finished_same_file_gates():
    assert roadmap.regression_gates(PR_TASK, ROAD) == ["pytest -q t.py::test_commit"]


def test_no_target_no_gates():
    assert roadmap.regression_gates("refactor things", ROAD) == []


def test_validate_reports_a_regression(monkeypatch):
    monkeypatch.setattr(state, "PROJECT_VALIDATE_COMMANDS", ["true"])
    ok, out = validation.validate(task_gate="true", regression_gates=["false"])
    assert not ok and out.startswith("REGRESSION")
    ok, _ = validation.validate(task_gate="true", regression_gates=["true"])
    assert ok
