"""Parallel-worker isolation: per-worker git worktrees + locked merges to main.

Guards the 2026-09-28 fix. Parallel workers used to share one working tree:
one worker's between-task `checkout -f` + `clean -fd` wiped another's
in-progress file, and merges collided. Also guards the language-agnostic
task grouping (it only recognised lib/*.dart, so Python same-file tasks were
dealt round-robin across workers).

Tests the real sov modules; config values are pinned per test.
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import files, gitops, roadmap, scheduling, state  # noqa: E402

_API = {
    gitops: ["_git", "_merge_lock", "_to_main", "_setup_worker_worktree",
             "_branch_start", "_branch_merge", "_branch_abandon", "_WORKTREE_SHARED"],
    roadmap: ["mark_done", "_mark_done_locked", "_commit_roadmap_mark"],
    scheduling: ["_task_primary_file", "_assign_chains"],
    files: ["all_source_files"],
}


def _load(integration_root=None) -> dict:
    state.INTEGRATION_ROOT = integration_root
    return {name: getattr(mod, name) for mod, names in _API.items() for name in names}


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    monkeypatch.setattr(gitops, "MAIN_BRANCH", "main")
    monkeypatch.setattr(gitops, "GIT_BRANCHES", True)
    monkeypatch.setattr(state, "INTEGRATION_ROOT", None)


def _sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def _worktree_root(tmp_path, monkeypatch):
    monkeypatch.setenv("XANADU_WORKTREE_ROOT", str(tmp_path / "wt-root"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _sh(root, "init", "-q", "-b", "main")
    _sh(root, "config", "user.email", "t@t")
    _sh(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".venv/\nlogs/\n")
    (root / "ROADMAP.md").write_text(
        "".join(f"- [ ] task {i}\n" for i in range(1, 21)))
    (root / "shared.py").write_text("x = 1\n")
    _sh(root, "add", "-A")
    _sh(root, "commit", "-qm", "init")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (root / "logs").mkdir()
    return root


def _workers(repo, n=2):
    ns = _load(str(repo))
    wts = [ns["_setup_worker_worktree"](str(repo), w) for w in range(n)]
    return ns, wts


def _status(path):
    return _sh(path, "status", "--porcelain").strip()


def test_worktree_shares_untracked_toolchain_and_stays_clean(repo):
    ns, (wt0, _) = _workers(repo)
    assert os.path.islink(os.path.join(wt0, ".venv"))
    assert os.path.exists(os.path.join(wt0, ".venv", "bin", "python"))
    assert os.path.islink(os.path.join(wt0, "logs"))
    assert _status(wt0) == ""
    assert _status(repo) == ""
    # clean -fd between tasks must not delete the shared symlinks
    ns["_branch_start"](wt0, 1, "task 1")
    assert os.path.islink(os.path.join(wt0, ".venv"))


def test_worker_sees_the_project_files(repo):
    """Regression: worktrees first lived under .git/, and all_source_files()
    skips any path containing ".git" — workers got no project files at all."""
    ns, (wt0, _) = _workers(repo)
    Path(repo, "pkg").mkdir()
    Path(repo, "pkg", "mod.py").write_text("y = 2\n")
    _sh(repo, "add", "-A")
    _sh(repo, "commit", "-qm", "add pkg")
    ns["_branch_start"](wt0, 1, "task 1")
    assert ".git" not in Path(wt0).parts
    assert ns["all_source_files"](wt0) == ns["all_source_files"](str(repo))
    assert "pkg/mod.py" in ns["all_source_files"](wt0)


def test_one_worker_reset_does_not_wipe_the_other(repo):
    ns, (wt0, wt1) = _workers(repo)
    b0 = ns["_branch_start"](wt0, 1, "task 1")
    b1 = ns["_branch_start"](wt1, 2, "task 2")
    Path(wt1, "b.py").write_text("b = 2\n")  # worker 1 mid-attempt
    Path(wt0, "a.py").write_text("a = 1\n")
    assert ns["_branch_merge"](wt0, b0, 1, "task 1") is True
    ns["mark_done"]("- [ ] task 1")
    ns["_branch_start"](wt0, 3, "task 3")  # worker 0 resets for its next task
    assert Path(wt1, "b.py").read_text() == "b = 2\n"
    assert ns["_branch_merge"](wt1, b1, 2, "task 2") is True
    ns["mark_done"]("- [ ] task 2")
    assert Path(repo, "a.py").exists() and Path(repo, "b.py").exists()
    roadmap = Path(repo, "ROADMAP.md").read_text()
    assert "- [x] task 1\n" in roadmap and "- [x] task 2\n" in roadmap
    assert _status(repo) == ""
    branches = _sh(repo, "branch")
    assert "task-1-" not in branches and "task-2-" not in branches  # task 3 is in progress


def test_merge_conflict_requeues_and_leaves_main_clean(repo):
    ns, (wt0, wt1) = _workers(repo)
    b0 = ns["_branch_start"](wt0, 1, "task 1")
    b1 = ns["_branch_start"](wt1, 2, "task 2")
    Path(wt0, "shared.py").write_text("x = 'worker0'\n")
    Path(wt1, "shared.py").write_text("x = 'worker1'\n")
    assert ns["_branch_merge"](wt0, b0, 1, "task 1") is True
    assert ns["_branch_merge"](wt1, b1, 2, "task 2") is False
    assert Path(repo, "shared.py").read_text() == "x = 'worker0'\n"
    assert _status(repo) == ""
    assert not Path(repo, ".git", "MERGE_HEAD").exists()
    assert _status(wt1) == ""


def test_already_satisfied_task_is_done_not_fatal(repo):
    ns, (wt0, _) = _workers(repo)
    b = ns["_branch_start"](wt0, 1, "task 1")
    Path(wt0, "shared.py").write_text("x = 1\n")  # identical to main
    assert ns["_branch_merge"](wt0, b, 1, "task 1") is True
    assert "task-" not in _sh(repo, "branch")


def test_abandon_discards_attempt_in_worktree_only(repo):
    ns, (wt0, _) = _workers(repo)
    b = ns["_branch_start"](wt0, 1, "task 1")
    Path(wt0, "junk.py").write_text("broken(\n")
    ns["_branch_abandon"](wt0, b)
    assert not Path(wt0, "junk.py").exists()
    assert os.path.islink(os.path.join(wt0, ".venv"))
    assert _status(repo) == ""


def test_concurrent_workers_land_every_task(repo):
    ns, wts = _workers(repo, n=3)
    errors = []

    def run(w):
        try:
            for k in range(4):
                idx = w * 4 + k + 1
                b = ns["_branch_start"](wts[w], idx, f"task {idx}")
                Path(wts[w], f"f{idx}.py").write_text(f"v = {idx}\n")
                assert ns["_branch_merge"](wts[w], b, idx, f"task {idx}")
                ns["mark_done"](f"- [ ] task {idx}")
        except BaseException as e:  # noqa: BLE001 — surface in main thread
            errors.append(e)

    threads = [threading.Thread(target=run, args=(w,)) for w in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    for idx in range(1, 13):
        assert Path(repo, f"f{idx}.py").exists(), idx
    roadmap = Path(repo, "ROADMAP.md").read_text()
    assert roadmap.count("- [x]") == 12
    assert _status(repo) == ""


def test_single_worker_mode_unchanged(repo):
    ns = _load(None)
    b = ns["_branch_start"](str(repo), 1, "task 1")
    assert _sh(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == b
    Path(repo, "a.py").write_text("a = 1\n")
    assert ns["_branch_merge"](str(repo), b, 1, "task 1") is True
    assert _sh(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_primary_file_is_language_agnostic():
    pf = _load()["_task_primary_file"]
    assert pf("- [ ] In `xmeasure/mappers/timeparse.py`: implement `f()`") \
        == "xmeasure/mappers/timeparse.py"
    assert pf("- [ ] In `src/sim/move.ts`: port `move`") == "src/sim/move.ts"
    assert pf("- [ ] Implement lib/sim/tick.dart tick()") == "lib/sim/tick.dart"


def test_same_file_tasks_stay_on_one_worker_even_split_or_nonadjacent():
    assign = _load()["_assign_chains"]
    t = lambda i, f: (i, f"- [ ] In `{f}`: task {i}")  # noqa: E731
    # a.py split into two chains (as MAX_CHAIN_LEN does), plus a later recurrence
    chains = [[t(1, "a.py"), t(2, "a.py")], [t(3, "a.py")], [t(4, "b.py")],
              [t(5, "c.py")], [t(6, "a.py")], [t(7, "b.py")]]
    by_worker = {w: [i for c in assign(chains, 2, w) for i, _ in c] for w in (0, 1)}
    assert sorted(by_worker[0] + by_worker[1]) == [1, 2, 3, 4, 5, 6, 7]
    a_owner = [w for w in (0, 1) if 1 in by_worker[w]][0]
    assert {1, 2, 3, 6} <= set(by_worker[a_owner])
    b_owner = [w for w in (0, 1) if 4 in by_worker[w]][0]
    assert 7 in by_worker[b_owner]
    assert by_worker[0] and by_worker[1]


def test_stale_empty_branch_is_rebuilt_from_current_main(repo):
    """A leftover task branch with nothing of its own must not pin an old main."""
    ns = _load(None)
    _sh(repo, "branch", "task-1-task-1")            # points at the initial commit
    Path(repo, "new.py").write_text("n = 1\n")
    _sh(repo, "add", "-A")
    _sh(repo, "commit", "-qm", "main moved on")
    ns["_branch_start"](str(repo), 1, "task 1")
    assert Path(repo, "new.py").exists()            # working on today's main


def test_branch_with_unmerged_work_is_reused(repo):
    ns = _load(None)
    _sh(repo, "checkout", "-q", "-b", "task-1-task-1")
    Path(repo, "work.py").write_text("w = 1\n")
    _sh(repo, "add", "-A")
    _sh(repo, "commit", "-qm", "committed, never merged")
    _sh(repo, "checkout", "-q", "main")
    ns["_branch_start"](str(repo), 1, "task 1")
    assert Path(repo, "work.py").exists()           # the committed work survives


def test_rag_index_survives_the_between_task_clean(repo):
    ns, (wt0, _) = _workers(repo)
    idx = Path(wt0, ".sovereign_rag")
    idx.mkdir()
    (idx / "chunks.lance").write_text("index")
    ns["_branch_start"](wt0, 1, "task 1")
    assert (idx / "chunks.lance").exists()          # rebuilt-every-task bug (2026-09-30)
