"""ROADMAP.md: parsing task lines, classifying test tasks, extracting task
gates, and marking tasks done (committed immediately, under the merge lock).

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
import fcntl
import os
import re
import time
from sov import state
from sov.gitops import _git, _merge_lock
from sov.ui import RED, RESET


ROADMAP_PATH   = "ROADMAP.md"


# ─── ROADMAP helpers ──────────────────────────────────────────────────────────

def parse_all_tasks() -> list[str]:
    """Return every unchecked task from ROADMAP.md, in order, regardless of date.
    Date headers are used only for human readability — they do not gate execution."""
    if not os.path.exists(ROADMAP_PATH):
        return []
    task_re = re.compile(r"^- \[ \] .+")
    return [
        line.strip()
        for line in open(ROADMAP_PATH)
        if task_re.match(line.strip())
    ]


def task_text(line: str) -> str:
    return re.sub(r"^- \[.\] ", "", line).strip()


# ─── Task type classification ─────────────────────────────────────────────────

_TEST_TASK_RE = re.compile(
    r'^(write|add)\s+(a\s+|an\s+)?(unit\s+|widget\s+|golden\s+|integration\s+)?tests?\b',
    re.IGNORECASE,
)

def _is_test_task(task: str) -> bool:
    """Return True if this task must only produce files under test/.

    Test tasks must never modify lib/ source files.  If the feature under test
    is missing they should write a skip-marked placeholder instead of
    implementing the feature themselves.
    """
    return bool(_TEST_TASK_RE.match(task.strip()))


def mark_done(task_line: str):
    """Mark a task done in ROADMAP.md. Uses an exclusive file lock so parallel
    workers cannot corrupt each other's writes.

    2026-07-13: the [x] mark MUST be committed immediately. mark_done() runs
    after _branch_merge() returns to main, so the mark sat UNCOMMITTED in the
    working tree — and the very next task's _branch_start() does a forced
    `git checkout -f main`, which resets every tracked file (ROADMAP.md
    included) to HEAD. Net effect: every DONE mark was erased seconds after
    it was written, and the remaining-task count never moved between runs.
    (Before the 2026-07-13 force-checkout fix the marks survived only by
    accident — the dirty tree leaked through plain checkouts and eventually
    got swept into a later task's `git add -A` commit.)
    """
    # Parallel workers: the mark goes to the primary checkout's ROADMAP (the
    # worktree's copy is a detached snapshot), under the merge lock.
    roadmap = os.path.join(state.INTEGRATION_ROOT, ROADMAP_PATH) if state.INTEGRATION_ROOT else ROADMAP_PATH
    with _merge_lock():
        _mark_done_locked(roadmap, task_line)


def _mark_done_locked(roadmap: str, task_line: str) -> None:
    with open(roadmap, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            lines = f.readlines()
            f.seek(0)
            for line in lines:
                if line.strip() == task_line:
                    f.write(line.replace("- [ ]", "- [x]", 1))
                else:
                    f.write(line)
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    _commit_roadmap_mark(task_line)


def _commit_roadmap_mark(task_line: str, attempts: int = 4) -> None:
    """Commit ROADMAP.md so the DONE mark survives forced checkouts.

    Retries briefly on index.lock contention (parallel workers). If every
    attempt fails the mark is still on disk — but the next _branch_start
    will wipe it, so a persistent failure is printed loudly.
    """
    cwd = state.INTEGRATION_ROOT or os.getcwd()
    if not os.path.exists(os.path.join(cwd, ".git")):
        return
    msg = f"roadmap: mark done — {task_text(task_line)[:60]}"
    for i in range(attempts):
        _git(["add", "ROADMAP.md"], cwd)
        r = _git(["commit", "-m", msg, "--", "ROADMAP.md"], cwd)
        if r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr):
            return
        time.sleep(0.5 * (i + 1))
    print(f"  {RED}⚠ could not commit ROADMAP.md mark — it WILL be lost at the "
          f"next forced checkout: {(r.stdout + r.stderr).strip()[:200]}{RESET}")


_TASK_GATE_RE = re.compile(r"task gate:\s*(.+?)(?:\s+passes)?\s*$", re.IGNORECASE)


def _extract_task_gate(task: str | None) -> str | None:
    """Pull the per-task acceptance command out of the task text.

    Tasks are authored with a trailing "task gate: <cmd> passes" clause (see
    plan_week.py). That command is the REAL acceptance test — typically a
    scoped conformance run like `npx vitest run conformance/pN.test.ts -t 'B05'`.
    validate_commands (typecheck + test:locked) pass on unimplemented stubs, so
    without running this gate a stub logs as PASSED. Returns the command, or
    None if the task carries no gate clause.
    """
    if not task:
        return None
    for line in task.splitlines():
        m = _TASK_GATE_RE.search(line.strip())
        if m:
            cmd = m.group(1).strip()
            return cmd or None
    return None
