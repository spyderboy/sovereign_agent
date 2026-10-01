"""Two timeouts with no code end the task and note it as over-scoped (2026-09-30)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from requests.exceptions import ReadTimeout  # noqa: E402

from sov import runner, state  # noqa: E402


def test_two_silent_timeouts_skip_and_note(tmp_path, monkeypatch):
    calls = []

    def silent(*a, **k):
        calls.append(1)
        raise ReadTimeout("generation exceeded 900s of wall clock (12 chars produced) — the model is streaming")

    monkeypatch.setattr(runner, "TIER_MODELS", ["m:27b"] * 4)
    monkeypatch.setattr(runner, "find_relevant_files", lambda t, r: [])
    monkeypatch.setattr(runner, "snapshot_baseline_errors", lambda r: [])
    monkeypatch.setattr(runner, "implement_task", silent)
    monkeypatch.setattr(runner, "CHRONIC_BLOCKER_THRESHOLD", 0)
    monkeypatch.setattr(state, "PROJECT_LANGUAGE", "")
    monkeypatch.setattr(state, "FINAL_TIER_ENABLED", False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    task = "In `pkg/big.py`: implement `everything()` — task gate: true passes"
    r = runner.run_task(task, str(tmp_path), str(tmp_path / "logs" / "w.log"), task_idx=1, skip_advisor=True)
    assert r == "skipped" and len(calls) == 2                     # not a third 15-minute attempt
    assert "pkg/big.py" in (tmp_path / "logs" / "needs_split.md").read_text()
