"""monitor.py: pattern detection, notifications and the guarded restart.

Drives the Monitor with synthetic console lines in the format supervisor.sh
writes ([wN]-prefixed, ANSI-coloured), against a scratch project.
"""
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, msg, urgent=True):
        self.sent.append(msg)


def _args(**kw):
    base = dict(restart=None, max_restarts=2, runaway_after=25, stall_after=40,
                skip_streak=4, no_merge_after=12, every=20, summary_every=0)
    base.update(kw)
    return Namespace(**base)


def _sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


ROADMAP = (
    "- [x] In `pkg/done.py`: implement `done()` — task gate: .venv/bin/python -m pytest -q "
    "\"tests/test_done.py::test_it\" passes\n"
    "- [ ] In `pkg/repos.py`: implement `parse_repos()` — task gate: .venv/bin/python -m pytest -q "
    "\"tests/test_repos.py::test_a\" passes\n"
    "- [ ] In `pkg/other.py`: implement `other()` — task gate: .venv/bin/python -m pytest -q "
    "\"tests/test_other.py\" passes\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "logs").mkdir()
    (root / "ROADMAP.md").write_text(ROADMAP)
    (root / "pkg" / "done.py").write_text("def done(): pass\n")
    for t in ("test_done.py", "test_repos.py", "test_other.py"):
        (root / "tests" / t).write_text("def test_it(): pass\n")
    monkeypatch.setattr(monitor, "newest_console", lambda r: None)
    return root


@pytest.fixture
def mon(project):
    clock = {"t": 1_000_000.0}
    m = monitor.Monitor(str(project), _args(), FakeNotifier(), clock=lambda: clock["t"])
    m._clock = clock
    return m


def feed(m, *lines):
    for line in lines:
        m.on_console(line)


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_split_worker_and_target():
    assert monitor.split_worker("[w1]   hello") == ("w1", "  hello")
    assert monitor.split_worker("plain") == ("w0", "plain")
    assert monitor.task_target("In `xmeasure/config/repos.py`: implement") == "xmeasure/config/repos.py"
    assert monitor.task_target("In lib/sim/tick.dart: port tick") == "lib/sim/tick.dart"


def test_error_signature_ignores_line_numbers():
    a = monitor.error_signature("test_config_repos.py:20: AssertionError")
    b = monitor.error_signature("test_config_repos.py:31: AssertionError")
    assert a == b


def test_gate_files_reads_every_line_for_the_target():
    assert monitor.gate_files(ROADMAP, "pkg/repos.py") == {"tests/test_repos.py"}
    assert monitor.gate_files(ROADMAP, "pkg/other.py") == {"tests/test_other.py"}


def test_classify_skips():
    assert monitor.classify_skips(["E1", "E1", "E1", "E2"]) == ("constant", "E1")
    assert monitor.classify_skips(["E1", "E2", "E3", "E4"]) == ("varied", None)


def test_phantom_done_handles_backticked_paths(project):
    assert monitor.phantom_done(str(project)) == []
    (project / "pkg" / "done.py").unlink()
    assert monitor.phantom_done(str(project)) == ["pkg/done.py"]


def test_tail_keeps_the_unfinished_line(tmp_path):
    p = tmp_path / "log"
    p.write_text("")
    t = monitor.Tail(str(p))
    p.write_text("\x1b[92mline one\x1b[0m\nAttempt 1/2 — coding (m)...")
    assert t.read() == ["line one"]
    assert t.partial.startswith("Attempt")
    with open(p, "a") as f:
        f.write(" wrote 1 file(s): x.py\n")
    assert t.read() == ["Attempt 1/2 — coding (m)... wrote 1 file(s): x.py"]


def test_process_matching_uses_real_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert monitor.mentions_project(f"bash ./sweep.sh {link}", os.path.realpath(real))
    assert not monitor.mentions_project(f"bash ./sweep.sh {tmp_path}/other", os.path.realpath(real))


# ── patterns ──────────────────────────────────────────────────────────────────

def test_repetition_alerts_once(mon):
    feed(mon, "[w0] [1/9] In `pkg/repos.py`: implement `parse_repos()`...")
    for n in (20, 21, 22, 23):
        feed(mon, f"[w0]   Validating... \x1b[91m✗ test_repos.py:{n}: AssertionError\x1b[0m")
    assert len([m for m in mon.notifier.sent if "same error" in m]) == 1


def test_varied_errors_do_not_alert_repetition(mon):
    feed(mon, "[w0] [1/9] In `pkg/repos.py`: implement `parse_repos()`...")
    for e in ("KeyError: 'a'", "TypeError: bad", "NameError: x"):
        feed(mon, f"[w0]   Validating... ✗ {e}")
    assert not mon.notifier.sent


def test_blind_context_alerts_after_two_blind_tasks(mon):
    feed(mon, "[w0] [1/9] In `pkg/repos.py`: implement `parse_repos()`...",
         "[w0]   Files: pkg/model.py, spec/00.md",
         "[w1] [1/9] In `pkg/other.py`: implement `other()`...",
         "[w1]   Files: pkg/model.py")
    assert any("without their tests" in m for m in mon.notifier.sent)


def test_context_with_tests_is_fine(mon):
    feed(mon, "[w0] [1/9] In `pkg/repos.py`: implement `parse_repos()`...",
         "[w0]   Files: pkg/model.py, tests/test_repos.py",
         "[w1] [1/9] In `pkg/other.py`: implement `other()`...",
         "[w1]   Files: tests/test_other.py")
    assert not mon.notifier.sent


def test_skip_streak_with_same_error_alerts(mon):
    for i, target in enumerate(("a.py", "b.py", "c.py", "d.py"), 1):
        feed(mon, f"[w1] [{i}/9] In `pkg/{target}`: implement x...",
             "[w1]   Validating... ✗ E   ModuleNotFoundError: No module named 'pkg.open'",
             "[w1]   ⏭  Skipped — queued for retry pass")
    assert any("skipped 4 tasks in a row" in m for m in mon.notifier.sent)


def test_skip_streak_with_varied_errors_does_not_alert(mon):
    for i, err in enumerate(("KeyError", "TypeError", "NameError", "no output"), 1):
        feed(mon, f"[w1] [{i}/9] In `pkg/t{i}.py`: implement x...",
             f"[w1]   Validating... ✗ {err}",
             "[w1]   ⏭  Skipped — queued for retry pass")
    assert not mon.notifier.sent


def test_merge_resets_the_skip_streak(mon):
    for i in (1, 2, 3):
        feed(mon, f"[w0] [{i}/9] In `pkg/t{i}.py`: x...", "[w0]   Validating... ✗ SAME",
             "[w0]   ⏭  Skipped — queued for retry pass")
    feed(mon, "[w0]   ✓ Marked done in ROADMAP.md")
    feed(mon, "[w0] [4/9] In `pkg/t4.py`: x...", "[w0]   Validating... ✗ SAME",
         "[w0]   ⏭  Skipped — queued for retry pass")
    assert not mon.notifier.sent


def test_runaway_attempt(mon, monkeypatch):
    monkeypatch.setattr(monitor, "run_pids", lambda r: [1])
    feed(mon, "[w0] [1/9] In `pkg/repos.py`: implement x...")
    mon.workers["w0"].attempt_at = mon._clock["t"]
    mon.console.grew_at = mon.sweep.grew_at = mon._clock["t"]
    mon._clock["t"] += 26 * 60
    mon.console.grew_at = mon.sweep.grew_at = mon._clock["t"]
    mon.periodic()
    assert any("one attempt on pkg/repos.py running 26 min" in m for m in mon.notifier.sent)


def test_fatal_line_alerts(mon):
    feed(mon, "[w0]   FATAL: git commit failed for task 1 after 4 attempts")
    assert any("FATAL" in m for m in mon.notifier.sent)


def test_replay_lists_history_without_notifying(mon):
    mon.replaying = True
    feed(mon, "[w0]   FATAL: something old")
    mon.replaying = False
    assert mon.history and not mon.notifier.sent


# ── stopped / finished / restart ─────────────────────────────────────────────

@pytest.fixture
def repo(project):
    _sh(project, "init", "-q", "-b", "main")
    _sh(project, "config", "user.email", "t@t")
    _sh(project, "config", "user.name", "t")
    (project / ".gitignore").write_text("logs/\n")
    _sh(project, "add", "-A")
    _sh(project, "commit", "-qm", "init")
    return project


def _dead(mon, monkeypatch):
    monkeypatch.setattr(monitor, "run_pids", lambda r: [])
    mon.periodic()
    mon.periodic()


def test_stopped_without_restart_alerts(mon, repo, monkeypatch):
    _dead(mon, monkeypatch)
    assert any("stopped unexpectedly" in m for m in mon.notifier.sent)


def test_finished_notifies_not_alerts(mon, repo, monkeypatch):
    mon.finished = True
    _dead(mon, monkeypatch)
    assert mon.notifier.sent == ["sweep finished: 1/3 done, 2 open"]


def test_parked_run_is_not_restarted(mon, repo, monkeypatch):
    (repo / "logs" / "supervisor.status").write_text("needs_fix\n")
    mon.args.restart = "MAIN_BRANCH=main"
    monkeypatch.setattr(monitor, "launch", lambda *a, **k: pytest.fail("restarted"))
    _dead(mon, monkeypatch)
    assert any("parked for a human" in m for m in mon.notifier.sent)


def test_restart_refuses_a_dirty_main_checkout(mon, repo, monkeypatch):
    (repo / "pkg" / "done.py").write_text("human edit\n")
    mon.args.restart = "MAIN_BRANCH=main"
    monkeypatch.setattr(monitor, "launch", lambda *a, **k: pytest.fail("restarted"))
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    _dead(mon, monkeypatch)
    assert any("NOT restarting" in m for m in mon.notifier.sent)
    assert (repo / "pkg" / "done.py").read_text() == "human edit\n"


def test_restart_cleans_stale_branches_and_relaunches(mon, repo, monkeypatch):
    _sh(repo, "branch", "task-7-stale")
    launched = {}

    def fake_launch(cmd, cwd, env, stdout):
        launched.update(cmd=cmd, cwd=cwd, env=env)

    mon.args.restart = "MAIN_BRANCH=main CHEAP=qwen25-t1 CHEAP_WORKERS=2"
    monkeypatch.setattr(monitor, "launch", fake_launch)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    _dead(mon, monkeypatch)
    assert launched["cmd"] == ["./sweep.sh", str(repo)]
    assert launched["env"]["CHEAP_WORKERS"] == "2"
    assert "task-7-stale" not in _sh(repo, "branch")
    assert any("restarted it" in m for m in mon.notifier.sent)


def test_restart_gives_up_after_max(mon, repo, monkeypatch):
    mon.args.restart = "MAIN_BRANCH=main"
    mon.restarts = [mon._clock["t"] - 60, mon._clock["t"] - 30]
    monkeypatch.setattr(monitor, "launch", lambda *a, **k: pytest.fail("restarted"))
    _dead(mon, monkeypatch)
    assert any("stopped restarting" in m for m in mon.notifier.sent)
