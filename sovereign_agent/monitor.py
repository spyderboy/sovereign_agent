#!/usr/bin/env python3
"""monitor.py — watch a run and say when something looks WRONG.

    python3 monitor.py ~/Code/witches_bricks
    python3 monitor.py ~/Code/witches_bricks --every 30

Run it in a second terminal beside a run. It prints a status line on a fixed
interval and, more importantly, raises a named alert the moment a known-bad
pattern appears, with the action that pattern calls for.

WHY THIS EXISTS

Every expensive failure on this project looked identical from the outside: a
worker running, logs growing, tasks not landing. The difference between "the
model is chewing on something hard" and "the harness is rejecting correct code
at every tier" is visible in the logs within two or three attempts — but only
if someone is reading them, and the tell is never in the last line.

These are the patterns that each cost hours, in the order they cost them:

  repetition        identical error across attempts and tiers → a constant in
                    the environment, not model weakness. Escalation cannot fix
                    it. (spawnRules, load.dart, validate_hints)
  guard-block       repeated bad-pattern blocks before validation ever runs →
                    a guard is probably rejecting correct code. Four separate
                    guards did this. (grounder: loop vars, lowercase members,
                    getters, typed params)
  foreign-blame     errors naming no file the task wrote → the cause landed in
                    an earlier merge; the failing task is innocent and no
                    ladder can reach the real bug. (seed.dart blamed 4 times)
  phantom-done      a task ticked [x] whose file is not on disk → the merge
                    discarded it, usually a locked prefix. Fifteen render files
                    vanished this way while every task reported PASSED.
  stall             no log growth → a stream trickling or a wedged server. The
                    watchdog fires at 45 min; you want to know at 10.
  no-merge          attempts happening, nothing landing → whatever is wrong is
                    systemic, not per-task.

Read-only: opens files, never writes, never touches git.
"""

import argparse
import os
import re
import time
from collections import Counter
from datetime import date

BOLD = "\033[1m"; RED = "\033[91m"; YEL = "\033[93m"; GRN = "\033[92m"
DIM = "\033[2m"; RESET = "\033[0m"

ALERT = f"{RED}⚠{RESET}"


def work_log_path(root: str) -> str:
    return os.path.join(root, "logs", f"{date.today().isoformat()}-work.log")


def counts(root: str) -> tuple[int, int]:
    p = os.path.join(root, "ROADMAP.md")
    if not os.path.exists(p):
        return 0, 0
    txt = open(p).read()
    return (len(re.findall(r"^- \[x\]", txt, re.M)),
            len(re.findall(r"^- \[[ x]\]", txt, re.M)))


def phantom_done(root: str) -> list[str]:
    p = os.path.join(root, "ROADMAP.md")
    out = []
    if not os.path.exists(p):
        return out
    for line in open(p):
        if line.startswith("- [x]"):
            m = re.search(r"In (\S+?):", line)
            if m and not os.path.exists(os.path.join(root, m.group(1))):
                out.append(m.group(1))
    return out


def parse_tail(root: str, since: int) -> tuple[list[dict], int]:
    """Return (per-task records, new byte offset) from the work log."""
    path = work_log_path(root)
    if not os.path.exists(path):
        return [], since
    size = os.path.getsize(path)
    if size < since:            # rotated or rewritten
        since = 0
    with open(path, errors="ignore") as f:
        f.seek(since)
        text = f.read()
    return text, size


def error_signature(block: str) -> list[str]:
    """The distinct analyzer/test errors in one attempt, normalised."""
    sigs = []
    for m in re.finditer(r"(?:error|warning) • (.+?) • ([\w./]+):(\d+)", block):
        sigs.append(f"{m.group(1)[:60]}|{m.group(2)}")
    for m in re.finditer(r"^\s{2}([A-Z][\w ]+Exception[^\n]{0,60})", block, re.M):
        sigs.append(m.group(1)[:60])
    return sigs


def files_named(block: str) -> set[str]:
    return set(re.findall(r"((?:lib|test)/[\w./-]+\.dart)", block))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--every", type=int, default=60, help="status interval (s)")
    args = ap.parse_args()
    root = os.path.abspath(os.path.expanduser(args.project))

    print(f"{BOLD}monitoring {root}{RESET}   (Ctrl-C to stop)\n")

    offset = os.path.getsize(work_log_path(root)) if os.path.exists(work_log_path(root)) else 0
    done0, total = counts(root)
    last_done = done0
    last_growth = time.time()
    last_merge = time.time()
    seen_alerts: set[str] = set()
    attempts_since_merge = 0

    def alert(key: str, msg: str, action: str) -> None:
        if key in seen_alerts:
            return
        seen_alerts.add(key)
        print(f"\n{ALERT} {BOLD}{msg}{RESET}\n   {DIM}{action}{RESET}\n", flush=True)

    while True:
        time.sleep(args.every)
        text, offset = parse_tail(root, offset)
        done, total = counts(root)
        now = time.strftime("%H:%M:%S")

        if text.strip():
            last_growth = time.time()

        # ── per-task analysis of whatever arrived this interval ──────────────
        blocks = re.split(r"^## Task ", text, flags=re.M)
        for b in blocks:
            tm = re.search(r"In (\S+?):", b)
            target = tm.group(1) if tm else None
            attempts = re.findall(r"### Attempt \d+(.*?)(?=### Attempt|\Z)", b, re.S)
            attempts_since_merge += len(attempts)

            sigs = [tuple(error_signature(a)) for a in attempts if a.strip()]
            sigs = [s for s in sigs if s]
            if len(sigs) >= 3 and len(set(sigs)) == 1:
                alert(f"rep:{target}",
                      f"{target}: the SAME error {len(sigs)} attempts running",
                      "Repetition means a constant in the environment, not model "
                      "weakness — escalating cannot help. Read the error and ask "
                      "whether a gate, guard or task text is wrong.")

            blocked = b.count("blocked bad pattern")
            if blocked >= 4:
                alert(f"guard:{target}",
                      f"{target}: {blocked} bad-pattern blocks, validation never ran",
                      "A guard may be rejecting correct code — four have. Write the "
                      "file by hand and run it past the guards before blaming the model.")

            if target and attempts:
                named = files_named("\n".join(attempts))
                if named and target not in named:
                    alert(f"foreign:{target}",
                          f"{target}: every error names some OTHER file "
                          f"({', '.join(sorted(named)[:3])})",
                          "The cause landed in an earlier merge. This task is "
                          "innocent and no ladder reaches the real bug — fix the "
                          "file the errors actually name.")

        # ── whole-run signals ────────────────────────────────────────────────
        if done > last_done:
            last_merge = time.time()
            attempts_since_merge = 0
            seen_alerts = {k for k in seen_alerts if not k.startswith("nomerge")}
            last_done = done

        idle_min = (time.time() - last_growth) / 60
        if idle_min > 10:
            alert(f"stall:{int(idle_min//10)}",
                  f"no log output for {idle_min:.0f} minutes",
                  "A stream trickling or a wedged server. The watchdog exits at 45 "
                  "min; check `ollama ps` and whether a model shows 'Stopping...' "
                  "while the worker still waits.")

        if attempts_since_merge >= 8:
            alert(f"nomerge:{attempts_since_merge//8}",
                  f"{attempts_since_merge} attempts since anything last merged",
                  "Systemic, not per-task. Check the validate command passes on a "
                  "clean tree, and that the active config unlocks the layer being "
                  "written.")

        ph = phantom_done(root)
        if ph:
            alert("phantom",
                  f"{len(ph)} task(s) ticked done with no file: {', '.join(ph[:3])}",
                  "The merge discarded them — usually the target sits under a "
                  "locked_prefix in the ACTIVE config. Stop; nothing after this "
                  "is trustworthy.")

        lock = os.path.join(root, "logs", "run.lock")
        live = "live" if os.path.exists(lock) else f"{YEL}no lock{RESET}"
        bar = f"{GRN}{done}{RESET}/{total}"
        print(f"{DIM}[{now}]{RESET} {bar}  {live}  "
              f"{DIM}idle {idle_min:.0f}m  attempts since merge {attempts_since_merge}{RESET}",
              flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
