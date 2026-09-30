"""Git operations for task isolation: per-task branches, merges to the
integration branch, and per-worker worktrees for parallel runs.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from datetime import date
import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from sov.config import (
    GIT_BRANCHES,
    MAIN_BRANCH,
)
from sov import state
from sov.ui import DIM, GREEN, RED, RESET, YELLOW


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a git command quietly. Never raises — caller checks returncode."""
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)

# Untracked directories a worktree needs from the primary checkout: logs must
# stay shared (tier2 queue, velocity, traces), and validate/gate commands
# reference in-tree toolchains like `.venv/bin/python`.
_WORKTREE_SHARED = ("logs", ".venv", "venv", "node_modules", ".dart_tool")


@contextlib.contextmanager
def _merge_lock():
    """Serialize writes to the integration branch across parallel workers.
    Not re-entrant — never nest (flock on a second fd would self-deadlock)."""
    if not state.INTEGRATION_ROOT:
        yield
        return
    with open(os.path.join(state.INTEGRATION_ROOT, ".git", "xanadu-merge.lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _to_main(root: str) -> None:
    """Forced checkout of the integration branch's tip. In a worker worktree
    this must be a detached checkout: git refuses to check out a branch that
    is already checked out in another worktree (the primary)."""
    if state.INTEGRATION_ROOT:
        _git(["checkout", "-f", "--detach", MAIN_BRANCH], root)
    else:
        _git(["checkout", "-f", MAIN_BRANCH], root)


def _setup_worker_worktree(primary: str, worker_id: int) -> str:
    """Create (or recreate) this worker's private worktree and return its path."""
    import atexit
    # Outside the project AND outside any directory name a file scanner
    # skips. The first version (2026-09-28) put worktrees under .git/, and
    # all_source_files() / the grounders skip any path whose components
    # include ".git" — so workers saw none of the project's own files and
    # coded blind. ~/.cache/xanadu-worktrees/<project>-<hash>/w<N> is safe.
    import hashlib
    real = os.path.realpath(primary)
    key = hashlib.sha1(real.encode()).hexdigest()[:8]
    base = os.environ.get("XANADU_WORKTREE_ROOT") or os.path.join(
        os.path.expanduser("~"), ".cache", "xanadu-worktrees")
    wt = os.path.join(base, f"{os.path.basename(real)}-{key}", f"w{worker_id}")
    _git(["worktree", "remove", "--force", wt], primary)
    if os.path.exists(wt):
        shutil.rmtree(wt)
    _git(["worktree", "prune"], primary)
    r = _git(["worktree", "add", "--detach", wt, MAIN_BRANCH], primary)
    if r.returncode != 0:
        print(f"{RED}✗ worker {worker_id}: could not create worktree: "
              f"{r.stderr.strip()[:300]}{RESET}")
        sys.exit(1)
    # Share untracked essentials by symlink, and exclude the symlinks without
    # a trailing slash — `.venv/` in .gitignore matches a directory, not a
    # symlink, so `clean -fd` would otherwise delete them between tasks.
    # info/exclude lives in the common git dir, so this is shared by the
    # primary too, where the anchored names are equally harmless.
    exclude_path = os.path.join(
        wt, _git(["rev-parse", "--git-path", "info/exclude"], wt).stdout.strip())
    try:
        with open(exclude_path) as f:
            existing = set(f.read().split())
    except OSError:
        existing = set()
    new_lines = []
    for name in _WORKTREE_SHARED:
        src = os.path.join(primary, name)
        if not os.path.exists(src) or os.path.exists(os.path.join(wt, name)):
            continue  # absent, or tracked (already checked out in the worktree)
        os.symlink(src, os.path.join(wt, name))
        if f"/{name}" not in existing:
            new_lines.append(f"/{name}")
    if new_lines:
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a") as f:
            f.write("\n" + "\n".join(new_lines) + "\n")
    atexit.register(lambda: _git(["worktree", "remove", "--force", wt], primary))
    print(f"  {DIM}worker {worker_id}: isolated worktree {wt}{RESET}")
    return wt


def _branch_start(project_root: str, task_idx: int, task: str) -> str | None:
    """Create an isolated branch for this task. Returns branch name or None if git unavailable."""
    if not GIT_BRANCHES:
        return None
    if not os.path.exists(os.path.join(project_root, ".git")):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", task[:45].lower()).strip("-")
    branch = f"task-{task_idx}-{slug}"
    # Always start from main so branches don't stack on each other.
    # MUST be a forced checkout: write_changes() writes the model's output
    # straight to disk before validation ever runs, so the working tree is
    # essentially always dirty by the time we get here. A plain `checkout
    # main` silently no-ops on a dirty tree instead of erroring — it does
    # NOT restore main's clean file contents — so a previous task's failed
    # (never-committed) writes were surviving in the working tree and
    # contaminating every task after it, even though the branches themselves
    # forked cleanly from main at the git-ref level. Found 2026-07-13: task
    # 4's abandoned squad_move.go (with a hallucinated import of a package
    # that doesn't exist) kept failing go vet for tasks 8 and 10, neither of
    # which had anything to do with squad_move.go.
    _to_main(project_root)
    # -e logs: never clean the logs/ dir — tier2_queue.jsonl, velocity.jsonl,
    # the failure ledger etc. live there untracked; a bare `clean -fd` was
    # deleting the --deep queue between tasks (2026-07-13).
    _git(["clean", "-fd", "-e", "logs", "-e", ".sovereign_rag"], project_root)
    r = _git(["checkout", "-b", branch], project_root)
    if r.returncode != 0:
        # Branch exists from a previous aborted run. Reuse it only if it holds
        # work of its own (committed but never merged); a branch with nothing
        # beyond main is just a stale pointer to an OLD main, and working on it
        # would validate against code main has since moved past (2026-09-28).
        ahead = _git(["rev-list", "--count", f"{MAIN_BRANCH}..{branch}"], project_root)
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            _git(["checkout", "-f", "-B", branch, MAIN_BRANCH], project_root)
        else:
            _git(["checkout", "-f", branch], project_root)
    return branch


def _branch_merge(project_root: str, branch: str, task_idx: int, task: str) -> bool:
    """Commit the task changes, merge to the integration branch, delete the branch.

    Returns False only in parallel mode when the merge conflicts with work
    another worker landed first: the merge is aborted, main stays clean, and
    the caller should requeue the task rather than mark it done.

    Every step here is load-bearing: if `commit` or `merge` silently fails
    (index.lock contention is the usual cause — seen 2026-08-21: a single
    un-retried commit here lost a brief lock race and nothing checked the
    return code, so two gate-passing tasks' worth of work sat uncommitted
    until the *next* task's `_branch_start` ran `git clean -fd` and wiped
    them — meanwhile `_commit_roadmap_mark`'s retry loop happened to outlast
    the same contention, so ROADMAP.md showed both tasks done while the code
    itself had silently vanished), the task must not be allowed to silently
    disappear. Retry briefly like `_commit_roadmap_mark` does, then fail
    loudly and stop rather than losing validated work.
    """
    def _run_with_retry(args: list[str], step_name: str, attempts: int = 4) -> subprocess.CompletedProcess:
        r = _git(args, project_root)
        for i in range(attempts - 1):
            if r.returncode == 0:
                return r
            time.sleep(0.5 * (i + 1))
            r = _git(args, project_root)
        if r.returncode != 0:
            print(f"  {RED}FATAL: git {step_name} failed for task {task_idx} after "
                  f"{attempts} attempts — refusing to continue and risk losing this "
                  f"task's work.{RESET}")
            print(f"  {DIM}{r.stderr.strip()}{RESET}")
            print(f"  Check for a stale .git/index.lock (lock contention or a prior "
                  f"killed git/work.py process can leave one behind) and remove it, "
                  f"then retry.")
            sys.exit(1)
        return r

    _run_with_retry(["add", "-A"], "add")
    # Nothing staged means main already satisfies this task — typically a
    # sibling task on the same file wrote this function too (2026-09-28:
    # xmeasure's parse_github_time landed inside the parse_jira_time task).
    # The gate just passed against that code, so the task is done: there is
    # nothing to commit or merge. `git commit` would exit 1 ("nothing to
    # commit", empty stderr) and the retry wrapper would kill the whole run.
    if _git(["diff", "--cached", "--quiet"], project_root).returncode == 0:
        print(f"  {DIM}Already satisfied on {MAIN_BRANCH} — nothing to commit{RESET}")
        _to_main(project_root)
        _git(["branch", "-D", branch], project_root)
        return True
    _run_with_retry(["commit", "-m", f"task {task_idx}: {task[:72]}"], "commit")

    if state.INTEGRATION_ROOT:
        # Parallel: merge in the primary checkout, which alone has main
        # checked out, while holding the merge lock.
        with _merge_lock():
            for i in range(4):
                merge_r = _git(["merge", "--no-ff", branch, "-m", f"Merge task-{task_idx}"],
                               state.INTEGRATION_ROOT)
                if merge_r.returncode == 0 or "index.lock" not in merge_r.stderr:
                    break
                time.sleep(0.5 * (i + 1))
            if merge_r.returncode != 0:
                _git(["merge", "--abort"], state.INTEGRATION_ROOT)
                _git(["checkout", "-f", MAIN_BRANCH], state.INTEGRATION_ROOT)
        _to_main(project_root)
        _git(["branch", "-D", branch], project_root)
        if merge_r.returncode != 0:
            print(f"  {YELLOW}⚠ merge into {MAIN_BRANCH} failed (conflict with another "
                  f"worker's merge?) — aborted, task requeued: "
                  f"{(merge_r.stdout + merge_r.stderr).strip()[:200]}{RESET}")
            return False
        return True

    _git(["checkout", "-f", MAIN_BRANCH], project_root)

    merge_r = _run_with_retry(
        ["merge", "--no-ff", branch, "-m", f"Merge task-{task_idx}"], "merge"
    )
    if merge_r.returncode != 0:
        # _run_with_retry already exited on failure; this branch is
        # unreachable but kept for clarity if that behavior ever changes.
        print(f"  Resolve manually: git checkout {MAIN_BRANCH} && git merge {branch}")
        sys.exit(1)

    _git(["branch", "-D", branch], project_root)
    return True


def _branch_abandon(project_root: str, branch: str) -> None:
    """Discard a failed task branch and return to main.

    Force + clean: this branch failed, so nothing it wrote — committed or
    not — should be allowed to survive into main or leak into the next
    task's working tree. See the note in _branch_start for why a plain
    (non-forced) checkout isn't enough here.
    """
    _to_main(project_root)
    _git(["clean", "-fd", "-e", "logs", "-e", ".sovereign_rag"], project_root)
    _git(["branch", "-D", branch], project_root)


def _post_planning_commit(project_root: str,
                          files: list[str] | None = None) -> bool:
    """Commit and push sprint planning artifacts so cloud workers can pull them.

    Called after plan_week.py (or standup) generates ROADMAP.md + task_graph.json.
    Only commits if there are actual staged changes — safe to call repeatedly.
    Returns True if everything succeeded (or there was nothing to commit).
    """
    if not os.path.exists(os.path.join(project_root, ".git")):
        print(f"  {YELLOW}No .git found in {project_root} — skipping sprint commit{RESET}")
        return False

    stage_targets = files or ["ROADMAP.md", "task_graph.json", ".roorules", "docs/"]
    existing = [f for f in stage_targets
                if os.path.exists(os.path.join(project_root, f))]
    if not existing:
        print(f"  {YELLOW}Nothing to commit (no planning files found){RESET}")
        return False

    _git(["add"] + existing, project_root)

    # Bail out cleanly if nothing actually changed
    staged = _git(["diff", "--cached", "--quiet"], project_root)
    if staged.returncode == 0:
        print(f"  {DIM}Sprint planning files unchanged — no commit needed{RESET}")
        return True

    today = date.today().isoformat()
    commit_r = _git(["commit", "-m", f"sprint planning: {today}"], project_root)
    if commit_r.returncode != 0:
        print(f"  {YELLOW}git commit failed: {commit_r.stderr.strip()}{RESET}")
        return False

    print(f"  {GREEN}✓ Sprint plan committed ({', '.join(existing)}){RESET}")

    push_r = _git(["push", "origin", "HEAD"], project_root)
    if push_r.returncode != 0:
        print(f"  {YELLOW}git push failed (no remote / auth issue?):\n"
              f"    {push_r.stderr.strip()[:120]}{RESET}")
        return False

    print(f"  {GREEN}✓ Sprint plan pushed to origin — cloud workers can pull{RESET}")
    return True
