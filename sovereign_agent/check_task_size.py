#!/usr/bin/env python3
"""check_task_size.py — refuse ROADMAP tasks that bundle more than one job.

    python3 check_task_size.py ~/Code/witches_bricks
    python3 check_task_size.py ~/Code/witches_bricks --all      # checked too
    python3 check_task_size.py ~/Code/witches_bricks --quiet    # exit code only

Import it from a project's preflight so authoring cannot skip it:

    from check_task_size import oversized
    for t in oversized(open("ROADMAP.md").read()):
        FAIL.append(t.message)

WHY THIS IS A GATE AND NOT A STYLE NOTE

A task is a prompt. A local model holds one contract in working memory at a
time, and a task carrying several obligations makes it drop one — usually the
constructor shape, because that is the least repeated fact in the text. The
failure does not look like overload. It looks like incompetence: the same
error, every attempt, every tier, so escalation cannot help and the model
looks incapable of something it does easily when asked alone.

Measured on Witch's Bricks, 2026-08-11:

    loadScenarios  — 1 parse + 12 validation rules in one function
                     10+ attempts across 3 tiers, 14 errors, never converged
    parseScenario  — the same work, split six ways, first task of the six
                     3 attempts, 6 -> 2 errors, converging

Same model, same day, same contract. The only variable was task size.

WHAT COUNTS AS ONE OBLIGATION

One function, or one class, or one value type — reaching one named gate. If
the task text needs "and", "then", or a numbered list to describe what to
build, that is the split point, and the conjunction is telling you where.

Length alone is NOT the signal, which is why this does not simply count
characters. A task naming fourteen constructor fields is long and singular;
a short task saying "implement X and Y" is two. Precision is free — it is
obligations that cost.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass

# `implement `int foo(...)` and `List<X> bar(...)`` -> two obligations.
#
# The type part is capped at three space-separated words. An unbounded `\s`
# class spans whole sentences and matches a backtick early in the line against
# a parenthesis late in it, inventing obligations named `it` and `end`.
_SIG = re.compile(r"`(?:[\w<>,\[\]?]+ ){0,2}[\w<>,\[\]?]+\s+(\w+)\s*\(")
_CLASS = re.compile(r"`class\s+(\w+)")
_STEPS = re.compile(r"\(\d\)")
_GATE = re.compile(r"task gate:\s*(.+?)\s*$")
_TARGET = re.compile(r"In (\S+?):")

MAX_SIGNATURES = 2      # a matched pair like legalXs/applyX is idiomatic here
MAX_STEPS = 3           # "(1) … (2) … (3) …" beyond this is a pipeline, not a task
# Semicolon-separated obligations. The dangerous task is not the one building
# several functions — that is obvious on sight and easy to split. It is the
# ONE function carrying a list of rules: `loadScenarios` was a single signature
# with twelve validation clauses, and it read as reasonable right up until it
# beat a 14B and a 35B identically. Five is where a rule list stops being a
# signature and starts being a checklist.
MAX_CLAUSES = 5


@dataclass
class Finding:
    target: str
    reasons: list[str]

    @property
    def message(self) -> str:
        return f"task {self.target} is oversized: " + "; ".join(self.reasons)


def _obligations(text: str) -> tuple[list[str], int, int, int]:
    body = text.split("— done when:")[0]
    classes = {m.group(1) for m in _CLASS.finditer(body)}
    sigs = sorted({m.group(1) for m in _SIG.finditer(body)} | classes)
    # ONE CLASS IS ONE OBLIGATION, however many methods it has. A class and its
    # methods land in one file and reach one gate, so splitting them is not
    # possible and flagging them is noise. Two classes in a task IS two files.
    if len(classes) == 1:
        sigs = sorted(classes)
    return sigs, len(_STEPS.findall(body)), body.count(";"), len(body)


def oversized(roadmap_text: str, include_done: bool = False) -> list[Finding]:
    out: list[Finding] = []
    for line in roadmap_text.split("\n"):
        if not (line.startswith("- [ ]") or (include_done and line.startswith("- [x]"))):
            continue
        tm = _TARGET.search(line)
        target = tm.group(1) if tm else line[:60]
        sigs, steps, clauses, length = _obligations(line)
        reasons = []
        if len(sigs) > MAX_SIGNATURES:
            reasons.append(f"{len(sigs)} things to build ({', '.join(sigs[:6])}) "
                           f"— one file per obligation")
        if steps > MAX_STEPS:
            reasons.append(f"{steps} numbered steps — that is a pipeline; give each "
                           f"step its own file and let the last one sequence them")
        if clauses > MAX_CLAUSES:
            reasons.append(f"{clauses} semicolon-separated obligations — split on "
                           f"the semicolons")
        if reasons:
            out.append(Finding(target, reasons))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--all", action="store_true", help="check completed tasks too")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    path = os.path.join(os.path.expanduser(args.project), "ROADMAP.md")
    if not os.path.exists(path):
        print(f"no ROADMAP.md at {path}")
        return 2

    found = oversized(open(path).read(), include_done=args.all)
    if not args.quiet:
        if not found:
            print("task size: OK — every task carries one obligation")
        else:
            print(f"{len(found)} OVERSIZED TASK(S):\n")
            for f in found:
                print(f"  {f.target}")
                for r in f.reasons:
                    print(f"      {r}")
            print("\n  Split each into one file per obligation, smallest useful "
                  "chunk.\n  Helpers may gate on the analyze command, but the LAST "
                  "task in a\n  chain must gate on the conformance test, or a "
                  "stubbed helper merges green.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
