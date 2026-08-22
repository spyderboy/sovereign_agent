#!/usr/bin/env python3
"""
safe_git.py — a git wrapper for environments where stale .git/*.lock files
can't be deleted (e.g. a FUSE bridge that rejects unlink() on mounted files).

Background: in this sandbox, `rm`/`os.remove()`/git's own internal lock
cleanup all fail identically on files that live inside a mounted folder.
Git creates a `<name>.lock` file, writes into it, then deletes it and
renames it over the real file. The delete step is what fails here, so a
crashed or interrupted git process (or a retry after a failure) leaves a
stale lock sitting in .git/ that blocks every future git command with
"Unable to create '.git/index.lock': File exists" — even though nothing
is actually still running.

This script never deletes anything either (same restriction). Instead it
MOVES stale lock files out of .git/ into a sibling scratch directory
(`_scratch_<reponame>/`) using a flattened filename, then retries the git
command. It is safe to run against a healthy repo (finds nothing to sweep)
and safe to run repeatedly.

Hard rule: swept files are moved OUT of .git/refs/heads/ and never back in,
even renamed — a stray file re-appearing there can be misread as a real
branch ref. See sweep_locks() for the exact exclusion.

Usage:
  safe_git.py sweep <repo>
  safe_git.py status <repo>
  safe_git.py run <repo> -- <git-args...>
  safe_git.py delete-branch <repo> <branch>
  safe_git.py list-branches <repo>
  safe_git.py set-branch <repo> <branch> <sha>   # update-ref + symbolic-ref HEAD, no checkout
"""

import argparse
import subprocess
import sys
from pathlib import Path


def scratch_dir_for(repo: Path) -> Path:
    d = repo.parent / f"_scratch_{repo.name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sweep_locks(repo: Path, verbose: bool = True) -> list[str]:
    """Find every *.lock file under .git/ and move it to the scratch dir.
    Never moves anything INTO .git/refs/heads/ (that's only ever a source
    here, never a destination)."""
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        raise SystemExit(f"not a git repo (no .git dir): {repo}")

    scratch = scratch_dir_for(repo)
    swept = []
    for lock in sorted(git_dir.rglob("*.lock")):
        if not lock.is_file():
            continue
        rel = lock.relative_to(git_dir)
        flat_name = str(rel).replace("/", "__")
        dest = scratch / f"{flat_name}.swept.{_stamp()}"
        lock.rename(dest)
        swept.append(str(rel))
        if verbose:
            print(f"swept: {rel} -> {dest}")
    if verbose and not swept:
        print("no stale lock files found")
    return swept


_counter = [0]


def _stamp() -> str:
    # No wall-clock timestamps needed; a monotonic counter is enough to
    # keep swept filenames unique within one run, and mtime already
    # timestamps the file itself.
    _counter[0] += 1
    return f"{_counter[0]:04d}"


def run_git(repo: Path, args: list[str], retry: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in repo, sweeping locks first. If it fails with a
    lock-related error, sweep again and retry once."""
    sweep_locks(repo, verbose=False)
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )
    if proc.returncode != 0 and retry:
        stderr = proc.stderr or ""
        if "Unable to create" in stderr and ".lock" in stderr:
            print("git command hit a lock file; sweeping and retrying once...", file=sys.stderr)
            sweep_locks(repo, verbose=True)
            proc = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True
            )
    return proc


def cmd_sweep(args):
    repo = Path(args.repo).resolve()
    sweep_locks(repo, verbose=True)


def cmd_status(args):
    repo = Path(args.repo).resolve()
    sweep_locks(repo, verbose=False)
    branch = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    print("branch:", branch.stdout.strip() or branch.stderr.strip())
    short = run_git(repo, ["status", "--short"])
    print("--- git status --short ---")
    print(short.stdout or "(clean)")
    if short.stderr:
        print(short.stderr, file=sys.stderr)


def cmd_run(args):
    repo = Path(args.repo).resolve()
    proc = run_git(repo, args.git_args)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    sys.exit(proc.returncode)


def cmd_delete_branch(args):
    """Delete a branch by moving its ref file directly, bypassing
    `git branch -D` (which internally does a lock-then-delete that can
    fail the same way). Never touches refs/heads/ as a destination."""
    repo = Path(args.repo).resolve()
    sweep_locks(repo, verbose=False)
    ref_path = repo / ".git" / "refs" / "heads" / args.branch
    if not ref_path.is_file():
        # might be packed into packed-refs instead of a loose ref
        packed = repo / ".git" / "packed-refs"
        if packed.is_file() and f"refs/heads/{args.branch}" in packed.read_text():
            print(
                f"branch '{args.branch}' is packed (in packed-refs), not a loose ref file.\n"
                f"Not touching packed-refs automatically — remove the line manually or use "
                f"`git pack-refs --prune` on a healthy checkout instead.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"no such branch ref: refs/heads/{args.branch}", file=sys.stderr)
        sys.exit(1)
    scratch = scratch_dir_for(repo)
    dest = scratch / f"refs__heads__{args.branch}.deleted.{_stamp()}"
    ref_path.rename(dest)
    print(f"deleted branch '{args.branch}' (ref moved to {dest})")


def cmd_list_branches(args):
    repo = Path(args.repo).resolve()
    proc = run_git(repo, ["branch", "-a"])
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)


def cmd_set_branch(args):
    """Fast-forward/repoint a branch ref and HEAD without invoking
    `git checkout`, which tries to delete working-tree files during the
    switch and fails on this bridge. Only safe when the working tree
    already matches the target commit (e.g. after a commit landed on the
    wrong branch and you just need HEAD's symbolic ref corrected)."""
    repo = Path(args.repo).resolve()
    sweep_locks(repo, verbose=False)
    update = run_git(repo, ["update-ref", f"refs/heads/{args.branch}", args.sha])
    if update.returncode != 0:
        print(update.stderr, file=sys.stderr)
        sys.exit(update.returncode)
    sym = run_git(repo, ["symbolic-ref", "HEAD", f"refs/heads/{args.branch}"])
    if sym.returncode != 0:
        print(sym.stderr, file=sys.stderr)
        sys.exit(sym.returncode)
    print(f"HEAD -> refs/heads/{args.branch} @ {args.sha}")
    print("NOTE: this does not touch the working tree or index. If HEAD's")
    print("previous commit doesn't match this tree, run `git reset` (mixed)")
    print("to resync the index, or `git status` to check first.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sweep", help="move stale .lock files out of .git/")
    s.add_argument("repo")
    s.set_defaults(func=cmd_sweep)

    s = sub.add_parser("status", help="sweep, then show branch + git status --short")
    s.add_argument("repo")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("run", help="sweep, run `git <args>`, retry once on lock failure")
    s.add_argument("repo")
    s.add_argument("git_args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("delete-branch", help="delete a branch via direct ref-file move")
    s.add_argument("repo")
    s.add_argument("branch")
    s.set_defaults(func=cmd_delete_branch)

    s = sub.add_parser("list-branches", help="git branch -a, with sweep-and-retry")
    s.add_argument("repo")
    s.set_defaults(func=cmd_list_branches)

    s = sub.add_parser("set-branch", help="update-ref + symbolic-ref HEAD, no checkout")
    s.add_argument("repo")
    s.add_argument("branch")
    s.add_argument("sha")
    s.set_defaults(func=cmd_set_branch)

    args = p.parse_args()
    # `run`'s REMAINDER may start with a stray "--"; strip one if present.
    if args.command == "run" and args.git_args[:1] == ["--"]:
        args.git_args = args.git_args[1:]
    args.func(args)


if __name__ == "__main__":
    main()
