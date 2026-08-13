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
    ap.add_argument("--every", type=int, default=15,
                    help="how often to poll the logs (s)")
    ap.add_argument("--quiet-after", type=int, default=10,
                    help="minutes of silence before saying so")
    args = ap.parse_args()
    root = os.path.abspath(os.path.expanduser(args.project))
    console = os.path.join("/tmp", f"{os.path.basename(root)}-run.log")

    print(f"{BOLD}watching {root}{RESET}  {DIM}(events only — silence means nothing "
          f"changed){RESET}\n", flush=True)

    wl = work_log_path(root)
    offset = os.path.getsize(wl) if os.path.exists(wl) else 0
    coffset = os.path.getsize(console) if os.path.exists(console) else 0
    done, total = counts(root)
    print(f"{DIM}start{RESET}  {GRN}{done}{RESET}/{total}", flush=True)

    last_event = time.time()
    last_task = None
    attempt_n = 0
    last_sig: tuple = ()
    repeats = 0
    blocked = 0
    attempts_since_merge = 0
    said_quiet = 0
    fired: set[str] = set()

    def stamp() -> str:
        return f"{DIM}{time.strftime('%H:%M')}{RESET}"

    def alert(key: str, msg: str, action: str) -> None:
        if key in fired:
            return
        fired.add(key)
        print(f"       {ALERT} {BOLD}{msg}{RESET}\n          {DIM}{action}{RESET}",
              flush=True)

    while True:
        time.sleep(args.every)
        text, offset = parse_tail(root, offset)
        ctext, coffset = (parse_tail_file(console, coffset)
                          if os.path.exists(console) else ("", coffset))

        for line in ctext.splitlines():
            if "Deferred" in line:
                dep = line.split("—")[-1].strip()
                print(f"{stamp()}  {DIM}⏭  deferred: {dep[:70]}{RESET}", flush=True)
                last_event = time.time()
            elif "Attempt" in line and "coding" in line:
                am = re.search(r"Attempt (\d+)/(\d+).*?coding \(([^)]+)\)", line)
                if am:
                    print(f"{stamp()}    {DIM}attempt {am.group(1)}/{am.group(2)} "
                          f"generating ({am.group(3)}){RESET}", flush=True)
                    last_event = time.time()
            elif "escalating to tier" in line:
                print(f"{stamp()}  {YEL}⚡ {line.strip()[:90]}{RESET}", flush=True)
                last_event = time.time()
            elif "blocked bad pattern" in line:
                blocked += 1
                why = re.sub(r".*blocked bad pattern: ", "", line).strip()[:80]
                print(f"{stamp()}    {YEL}blocked{RESET} {DIM}{why}{RESET}", flush=True)
                last_event = time.time()
                if blocked >= 4:
                    alert(f"guard:{last_task}",
                          f"{blocked} bad-pattern blocks — validation never ran",
                          "A guard may be rejecting correct code; four have. Write "
                          "the file by hand and run it past the guards.")
            elif "STALLED" in line or "stalled and exited" in line:
                print(f"{stamp()}  {RED}⏱  worker stalled{RESET}", flush=True)
                last_event = time.time()

        for raw in text.splitlines():
            m = re.match(r"## Task \d+: In (\S+?):", raw)
            if m:
                last_task = m.group(1)
                attempt_n, last_sig, repeats, blocked = 0, (), 0, 0
                print(f"{stamp()}  {BOLD}▶ {last_task}{RESET}", flush=True)
                last_event = time.time()
                continue
            if raw.startswith("### Attempt"):
                attempt_n = int(re.search(r"\d+", raw).group())
                continue
            if raw.startswith("Validation:"):
                ok_ = "PASSED" in raw
                if ok_:
                    done, total = counts(root)
                    attempts_since_merge = 0
                    fired = {k for k in fired if not k.startswith("nomerge")}
                    print(f"{stamp()}  {GRN}✓ {last_task} merged{RESET}  "
                          f"{GRN}{done}{RESET}/{total}", flush=True)
                else:
                    attempts_since_merge += 1
                last_event = time.time()
                continue
            em = re.match(r"\s+(?:error|warning) • (.+?) •", raw)
            if em and attempt_n:
                sig = (em.group(1)[:70],)
                same = sig == last_sig
                repeats = repeats + 1 if same else 1
                last_sig = sig
                tag = f"{DIM}(same){RESET}" if same else ""
                print(f"{stamp()}    attempt {attempt_n} {RED}failed{RESET}  "
                      f"{sig[0]} {tag}", flush=True)
                if repeats >= 3:
                    alert(f"rep:{last_task}:{sig[0][:20]}",
                          f"same error {repeats} attempts running",
                          "Repetition means a constant in the environment, not "
                          "model weakness — escalation cannot fix it. Check the "
                          "gate, the guards and the task text.")
                if last_task:
                    named = files_named(raw)
                    if named and last_task not in named:
                        alert(f"foreign:{last_task}",
                              f"errors name {', '.join(sorted(named)[:2])}, not "
                              f"{last_task}",
                              "The cause landed in an earlier merge. This task is "
                              "innocent — fix the file the errors name.")
                attempt_n = 0

        if attempts_since_merge >= 8:
            alert(f"nomerge:{attempts_since_merge // 8}",
                  f"{attempts_since_merge} attempts since anything merged",
                  "Systemic, not per-task. Check the validate command passes on a "
                  "clean tree and that the active config unlocks this layer.")

        ph = phantom_done(root)
        if ph:
            alert("phantom",
                  f"{len(ph)} task(s) ticked done with no file: {', '.join(ph[:3])}",
                  "The merge discarded them — usually a locked_prefix in the "
                  "ACTIVE config. Stop; nothing after this is trustworthy.")

        quiet = (time.time() - last_event) / 60
        if quiet >= args.quiet_after and int(quiet) // args.quiet_after > said_quiet:
            said_quiet = int(quiet) // args.quiet_after
            live = "worker alive" if os.path.exists(
                os.path.join(root, "logs", "run.lock")) else f"{YEL}no run.lock{RESET}"
            print(f"{stamp()}  {YEL}… {quiet:.0f} min with no events{RESET} "
                  f"{DIM}({live}; watchdog exits at 45){RESET}", flush=True)
        elif quiet < 1:
            said_quiet = 0


def parse_tail_file(path: str, since: int) -> tuple[str, int]:
    size = os.path.getsize(path)
    if size < since:
        since = 0
    with open(path, errors="ignore") as f:
        f.seek(since)
        return f.read(), size


if __name__ == "__main__":
    raise SystemExit(main())
