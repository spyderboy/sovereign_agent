"""Workers must call the functions their task names, not copy them (2026-10-01)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sov.contract_use import unused_contract  # noqa: E402

TASK = ("In `pkg/compare_all.py`: implement `compare_windows(b, p) -> tuple` — pair results by "
        "metric_id with compare_metric. Never call `legacy_compare`. — done when: its tests pass")


def _proj(tmp_path, body):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "compare.py").write_text("def compare_metric(b, p):\n    return 1\n"
                                                 "def legacy_compare(b, p):\n    return 0\n")
    (tmp_path / "pkg" / "compare_all.py").write_text(body)
    return str(tmp_path)


def test_a_copy_is_flagged(tmp_path):
    root = _proj(tmp_path, "def compare_windows(b, p):\n    return tuple(x - y for x, y in zip(b, p))\n")
    msg = unused_contract(TASK, ["pkg/compare_all.py"], root)
    assert msg and "`compare_metric`" in msg and "legacy_compare" not in msg   # negated mention skipped


def test_calling_it_passes(tmp_path):
    root = _proj(tmp_path, "from pkg.compare import compare_metric\n"
                           "def compare_windows(b, p):\n    return tuple(compare_metric(x, y) for x, y in zip(b, p))\n")
    assert unused_contract(TASK, ["pkg/compare_all.py"], root) is None


def test_functions_in_the_target_file_itself_do_not_count(tmp_path):
    root = _proj(tmp_path, "def run_collect(c):\n    return 1\n")
    task = ("In `pkg/compare_all.py`: implement `run_report(c) -> int` — keep run_collect as it is. "
            "— done when: its tests pass")
    assert unused_contract(task, ["pkg/compare_all.py"], root) is None
