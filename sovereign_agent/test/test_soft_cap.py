"""Soft pass cap: sweep.sh flags logs/stop_after_task; workers finish the task
in hand and start no new one (2026-09-30)."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import work  # noqa: E402

HERE = Path(__file__).resolve().parents[1]


def test_flag_in_the_primary_logs_stops_workers(tmp_path):
    (tmp_path / "logs").mkdir()
    args = SimpleNamespace(project=str(tmp_path))
    assert not work._stop_requested(args)
    (tmp_path / "logs" / "stop_after_task").touch()
    assert work._stop_requested(args)


def test_sweep_sets_and_clears_the_flag():
    src = (HERE / "sweep.sh").read_text()
    assert 'touch "$STOP_FLAG"' in src and src.count('rm -f "$STOP_FLAG"') == 2
    assert subprocess.run(["bash", "-n", str(HERE / "sweep.sh")]).returncode == 0
