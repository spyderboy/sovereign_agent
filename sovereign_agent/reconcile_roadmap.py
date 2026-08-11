#!/usr/bin/env python3
"""
reconcile_roadmap.py — restore DONE marks that were wiped by forced checkouts.

Background (2026-07-13): mark_done() wrote [x] into ROADMAP.md but never
committed it; the next task's `git checkout -f main` reset the file to HEAD,
erasing the mark. The WORK ITSELF IS FINE — every completed task was merged
to main with a commit message of the form:

    task <idx>: <first 72 chars of the task text>
    Merge task-<idx>

This script replays those commit messages against ROADMAP.md, marks the
matching unchecked tasks [x], and commits the result. Safe to run repeatedly.

Usage:
    python reconcile_roadmap.py ~/Code/GalaxicanGo            # apply
    python reconcile_roadmap.py ~/Code/GalaxicanGo --dry-run  # preview only
"""

import argparse
import os
import re
import subprocess
import sys


def git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", help="project root containing ROADMAP.md and .git")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.project))
    roadmap = os.path.join(root, "ROADMAP.md")
    if not os.path.exists(roadmap):
        print(f"No ROADMAP.md in {root}")
        return 1
    if not os.path.exists(os.path.join(root, ".git")):
        print(f"No .git in {root}")
        return 1

    # every "task N: <text>" subject ever merged to main
    r = git(["log", "main", "--pretty=%s"], root)
    if r.returncode != 0:
        print(f"git log failed: {r.stderr.strip()}")
        return 1
    merged: list[str] = []
    for subj in r.stdout.splitlines():
        m = re.match(r"^task \d+: (.+)$", subj.strip())
        if m:
            merged.append(m.group(1).strip())

    if not merged:
        print("No 'task N:' commits found on main — nothing to reconcile.")
        return 0

    with open(roadmap) as f:
        lines = f.readlines()

    changed = 0
    out_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- [ ] "):
            text = stripped[len("- [ ] "):].strip()
            # Commit subjects were truncated to 72 chars of the task text —
            # and git strips trailing whitespace from subject lines, so a
            # truncation landing on a space yields a 71-char subject. Compare
            # rstripped prefixes on both sides.
            prefix = text[:72].rstrip()
            if any(prefix == m[:72].rstrip() for m in merged if m):
                print(f"  ✓ {text[:76]}")
                line = line.replace("- [ ]", "- [x]", 1)
                changed += 1
        out_lines.append(line)

    unchecked_after = sum(1 for l in out_lines if l.strip().startswith("- [ ] "))
    print(f"\n{changed} task(s) recovered as DONE · {unchecked_after} still pending")

    if args.dry_run or changed == 0:
        if args.dry_run:
            print("(dry run — nothing written)")
        return 0

    with open(roadmap, "w") as f:
        f.writelines(out_lines)
    git(["add", "ROADMAP.md"], root)
    r = git(["commit", "-m",
             f"roadmap: reconcile {changed} DONE mark(s) lost to forced checkouts",
             "--", "ROADMAP.md"], root)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        print(f"⚠ commit failed — marks are on disk but UNCOMMITTED:\n{r.stderr.strip()}")
        return 1
    print("Committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
