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
# A keyword where the return type would be is a CALL in an example, not a signature to build:
# `throw const DomainError(x)` or `await findTask(r, id)` read as three things to build and flagged
# five well-sized magic_task_hat tasks (2026-10-07).
_SIG = re.compile(r"`(?!(?:throw|const|await|return|final|var|new|yield|if|else)\b)"
                  r"(?:(?!(?:throw|const|await|return|new)\b)[\w<>,\[\]?]+ ){0,2}"
                  r"(?!(?:throw|const|await|return|new)\b)[\w<>,\[\]?]+\s+(\w+)\s*\(")
_CLASS = re.compile(r"`class\s+(\w+)")
# A BARE BACKTICKED NAME IS STILL AN OBLIGATION. The effects task listed five —
# `paintChargeRibbons`, `paintLightningBeam`, `paintBlastRing`, `paintEmbers`,
# `paintShatter` — with no parentheses and no return types, so _SIG matched none
# of them and the task scored as ONE obligation. Five painters in one file under
# a 150-line limit is roughly 25 lines each including hashed bolt forks and
# shard fans; the model correctly tried to split the file and the harness
# rejected the split as an invented file, leaving it nowhere to go for ten
# attempts (2026-08-15).
#
# Restricted to verb-shaped lowerCamel names so that a task merely MENTIONING a
# type or a constant in backticks does not read as an instruction to build it.
_BARE_FN = re.compile(
    r"`((?:paint|draw|build|make|render|compute|apply|resolve|sample|paint)"
    r"[A-Z]\w*)`")
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

# A VAGUE CALLEE IS A HIDDEN OBLIGATION PER CALL. xmeasure's compute_window
# (2026-09-30) was one signature and three semicolons — it passed every count
# above — but said "call that metric's function from xmeasure.metrics.*" over
# 17 metrics. The model had to recall seventeen signatures; qwen3.8 thought for
# 15 minutes twice without emitting a character. Split, with every call written
# out literally, it became four small tasks. Any instruction to call a function
# the text does not name (and show the arguments of) is flagged.
_VAGUE_CALL = re.compile(
    r"\b(?:call|calls|use|uses|invoke|dispatch(?:es)? to)\s+"
    r"(?:that|each|its|their|every)\s+"     # per-item; "the cached function" refers back to a named one
    r"(?:\w+(?:'s)?\s+){0,3}?(?:function|handler|method|implementation|helper)s?\b",
    re.I)
_VAGUE_WHICH = re.compile(r"\b(?:the\s+)?(?:corresponding|appropriate|matching|respective)\s+"
                          r"(?:\w+\s+){0,2}?(?:function|handler|method|metric)s?\b", re.I)


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
    sigs = sorted({m.group(1) for m in _SIG.finditer(body)}
                  | {m.group(1) for m in _BARE_FN.finditer(body)}
                  | classes)
    # ONE CLASS IS ONE OBLIGATION, however many methods it has. A class and its
    # methods land in one file and reach one gate, so splitting them is not
    # possible and flagging them is noise. Two classes in a task IS two files.
    if len(classes) == 1:
        sigs = sorted(classes)
    # A semicolon inside `backticks` is Dart punctuation, not a clause boundary.
    # Task text showing `import 'dart:ui';` alongside two other imports read as
    # three separate obligations and reported three well-sized tasks oversized
    # (2026-08-13). Count clauses in the PROSE only.
    prose = re.sub(r"`[^`]*`", "", body)
    return sigs, len(_STEPS.findall(body)), prose.count(";"), len(body)


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
        prose = re.sub(r"`[^`]*`", "", line.split("— done when:")[0])
        vague = _VAGUE_CALL.search(prose) or _VAGUE_WHICH.search(prose)
        if vague:
            reasons.append(f'calls functions it does not name ("{vague.group(0)}") — write '
                           f"every call out with its arguments; if there are many, split "
                           f"them into helpers by group")
        if reasons:
            out.append(Finding(target, reasons))
    return out


_EDIT_PAIR = re.compile(r"REPLACE\s+`[^`]+`\s+WITH\s+`", re.S)
# The task's file: "In lib/x.dart:" or "... in lib/x.dart —" (both styles are in use).
_ANY_TARGET = re.compile(r"\b[Ii]n `?([\w.-]+(?:/[\w.-]+)+\.\w+)`?")


def prose_edits(roadmap_text: str, project_root: str) -> list[Finding]:
    """Open tasks that change an EXISTING file but describe the change in prose.

    "Edit this screen in six places" made the 27B rewrite whole EstateWise screens from memory, inventing
    fields and imports, attempt after attempt (2026-10-19). An edit task must spell out each change as
    REPLACE `exact current lines` WITH `new lines` — the harness then makes the model return those as
    SEARCH/REPLACE blocks, so it copies instead of recalling. New files are unaffected."""
    out: list[Finding] = []
    for line in roadmap_text.split("\n"):
        if not line.startswith("- [ ]"):
            continue
        tm = _TARGET.search(line) or _ANY_TARGET.search(line)
        if not tm or not os.path.isfile(os.path.join(project_root, tm.group(1))):
            continue
        if not _EDIT_PAIR.search(line):
            out.append(Finding(tm.group(1), [
                "edits an existing file in prose — write every change as REPLACE `exact current "
                "lines` WITH `new lines` (copied from the file), one pair per change"]))
    return out


# A TASK CAN BE ONE OBLIGATION AND STILL TOO BIG TO WRITE IN ONE GO. EstateWise step 2 (2026-10-26):
# every count above passed NoteFormScreen, one class, yet its reference file is 181 lines and its task text
# 4,866 characters. The 27B timed out without a word twice and every later task depended on it; PeopleScreen
# (105 lines) did the same. Across the step-2 screens, every task whose reference file reached 100 lines, or
# whose text passed ~2,500 characters, failed at least once; everything under ~90 lines merged first time.
# This is a WARNING: some long tasks do pass (RelationshipFormScreen, 120 lines). Split the file into a
# widget per section, or plan to write it by hand.
MAX_REFERENCE_LINES = 100
MAX_TASK_CHARS = 2500


def too_long(roadmap_text: str, project_root: str, reference_dir: str | None = None) -> list[Finding]:
    """Open tasks likely too big to write in one model call: a long reference file, or a long task text.

    Exact-pair edits are left out: the harness applies those itself (sov/pairs.py), however long."""
    out: list[Finding] = []
    for line in roadmap_text.split("\n"):
        if not line.startswith("- [ ]") or _EDIT_PAIR.search(line):
            continue
        tm = _TARGET.search(line) or _ANY_TARGET.search(line)
        target = tm.group(1).strip("`") if tm else line[6:60]
        reasons = []
        if reference_dir and tm:
            ref = os.path.join(reference_dir, target)
            if os.path.isfile(ref):
                n = sum(1 for _ in open(ref, encoding="utf-8", errors="ignore"))
                if n >= MAX_REFERENCE_LINES:
                    reasons.append(f"the reference file is {n} lines (warn at {MAX_REFERENCE_LINES}) — split it "
                                   f"into smaller widgets or helpers, or plan to write it by hand")
        body = _GATE.sub("", line[6:])
        if len(body) >= MAX_TASK_CHARS:
            reasons.append(f"the task text is {len(body):,} characters (warn at {MAX_TASK_CHARS:,}) — a model "
                           f"that must hold all of it at once tends to time out without output")
        if reasons:
            out.append(Finding(target, reasons))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--all", action="store_true", help="check completed tasks too")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--reference", metavar="DIR", help="a reference solution: warn on tasks whose file there is long")
    args = ap.parse_args()

    path = os.path.join(os.path.expanduser(args.project), "ROADMAP.md")
    if not os.path.exists(path):
        print(f"no ROADMAP.md at {path}")
        return 2

    text = open(path).read()
    found = oversized(text, include_done=args.all) + prose_edits(text, os.path.dirname(path))
    long_tasks = too_long(text, os.path.dirname(path),
                          os.path.expanduser(args.reference) if args.reference else None)
    if not args.quiet and long_tasks:
        print(f"{len(long_tasks)} LONG TASK(S) (warning — may time out without output):\n")
        for f in long_tasks:
            print(f"  {f.target}")
            for r in f.reasons:
                print(f"      {r}")
        print()
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
