#!/usr/bin/env python3
"""monitor.py — watch a run, tell you when it needs you, restart it if it dies.

    python3 monitor.py ~/Code/xmeasure
    python3 monitor.py ~/Code/xmeasure --ntfy-topic my-private-topic
    python3 monitor.py ~/Code/xmeasure --restart "MAIN_BRANCH=main CHEAP=qwen25-t1 STRONG=qwen38 CHEAP_WORKERS=2 STRONG_WORKERS=1"
    python3 monitor.py ~/Code/xmeasure --once        # one snapshot, then exit

Leave it running in a spare terminal (or under nohup). It prints events as
they happen, and raises an ALERT, with the action the pattern calls for, the
moment a known-bad pattern appears. Alerts also go out as a macOS notification
and, with --ntfy-topic, as a push to your phone via ntfy.sh. Silence means
nothing needs you.

WHY THIS EXISTS

Every expensive failure looked identical from the outside: a worker running,
logs growing, tasks not landing. The difference between "the model is chewing
on something hard" and "the harness is broken" is visible in the logs within a
few attempts — but only if someone is reading them. The patterns, each of which
has cost hours:

  repetition     identical error across attempts on one task → a constant in
                 the environment, not model weakness. Escalation cannot fix it.
  guard-block    repeated bad-pattern blocks before validation ever runs → a
                 guard is probably rejecting correct code. Four guards did this.
  foreign-blame  errors naming a source file the task didn't write → the cause
                 landed in an earlier merge; this task is innocent.
  phantom-done   a task ticked [x] whose file is not on disk → the merge
                 discarded it (usually a locked prefix).
  blind-context  the worker's context is missing the test its gate runs →
                 it is coding blind. (2026-09-28: worktrees under .git/ hid
                 every project file from file discovery; 0 of 4 tasks passed.)
  runaway        one attempt generating far longer than it should → a model
                 that won't stop. (2026-09-28: qwen3.8 ran 30 min on one.)
  skip-streak    a worker skipping task after task → if the errors match, a
                 constant cause; if they vary, the model is out of its depth.
  no-merge       attempts happening, nothing landing → systemic.
  stall          no log growth at all → wedged process or server.
  stopped        no run processes, roadmap unfinished, no clean finish logged.
  stale-tree     leftover worker worktrees or task branches after a run.

AUTOMATIC ACTION (only with --restart)

One action, because it is safe and reversible: restarting a run that STOPPED
unexpectedly. It never discards work — if the main checkout has any
uncommitted change it refuses and alerts instead (worker debris lives in the
worktrees, so a dirty main checkout means a human edit). It will not restart
a run the supervisor parked for a human (needs_fix / stuck), and gives up
after --max-restarts. It does NOT kill runaway attempts: killing work.py
stops the whole pass; the per-attempt call_timeout_s in the profile is what
bounds those.

Otherwise read-only: opens files, lists processes, reads git state.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

BOLD = "\033[1m"; RED = "\033[91m"; YEL = "\033[93m"; GRN = "\033[92m"
DIM = "\033[2m"; RESET = "\033[0m"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SOVEREIGN = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SOVEREIGN)                   # for sov.procs when run from elsewhere

FINISH_MARKERS = ("sweep finished", "fixpoint reached", "roadmap complete")
SKIP_MARKERS = ("Skipped — queued", "Still skipped", "Still failing",
                "Continuing to next task", "Dependency-blocked")
SRC_PATH = re.compile(r"([\w.-]+(?:/[\w.-]+)*\.(?:py|dart|ts|tsx|js|go|sh))\b")


# ─── Pure helpers (unit-tested) ───────────────────────────────────────────────

def strip_ansi(s: str) -> str:
    return ANSI.sub("", s)


def split_worker(line: str) -> tuple[str, str]:
    """'[w1]   text' → ('w1', 'text'); single-worker lines → ('w0', text)."""
    m = re.match(r"\[(w\d+)\]\s?(.*)$", line)
    return (m.group(1), m.group(2)) if m else ("w0", line)


def task_target(task_text: str) -> str | None:
    m = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", task_text)
    return m.group(1) if m else None


def error_signature(msg: str) -> str:
    """Normalise a validator's first error line so the same failure matches
    across attempts: drop line numbers, addresses and long paths."""
    s = re.sub(r":\d+", ":N", msg.strip())
    s = re.sub(r"0x[0-9a-f]+", "0x…", s)
    s = re.sub(r"/[\w./-]{25,}", "/…", s)
    return s[:90]


def gate_files(roadmap_text: str, target: str) -> set[str]:
    """Test files named by the task gates of every ROADMAP line targeting
    `target` (the console truncates task text, so match on the target)."""
    out: set[str] = set()
    for line in roadmap_text.splitlines():
        if f"`{target}`" not in line and f"In {target}:" not in line:
            continue
        g = re.search(r"task gate:\s*(.+?)(?:\s+passes)?\s*$", line)
        if not g:
            continue
        for path in SRC_PATH.findall(g.group(1)):
            if "test" in path:
                out.add(path.split("::")[0])
    return out


def classify_skips(errors: list[str]) -> tuple[str, str | None]:
    """('constant', sig) when most skipped tasks failed the same way, else
    ('varied', None). Repetition means diagnose; variation means escalate."""
    sigs = [e for e in errors if e]
    if not sigs:
        return "varied", None
    sig, n = Counter(sigs).most_common(1)[0]
    return ("constant", sig) if n >= 3 and n * 2 > len(sigs) else ("varied", None)


def phantom_done(root: str) -> list[str]:
    p = os.path.join(root, "ROADMAP.md")
    out = []
    if not os.path.exists(p):
        return out
    for line in open(p, errors="ignore"):
        if line.startswith("- [x]"):
            t = task_target(line)
            if t and not os.path.exists(os.path.join(root, t)):
                out.append(t)
    return out


def counts(root: str) -> tuple[int, int]:
    p = os.path.join(root, "ROADMAP.md")
    if not os.path.exists(p):
        return 0, 0
    txt = open(p, errors="ignore").read()
    return (len(re.findall(r"^- \[x\]", txt, re.M)),
            len(re.findall(r"^- \[[ x]\]", txt, re.M)))


# ─── Log tailing ──────────────────────────────────────────────────────────────

class Tail:
    """Complete new lines from a growing file; remembers the unfinished last
    line (a model attempt prints its result on the same line when done)."""

    def __init__(self, path: str | None, from_start: bool = False):
        self.path = path
        self.offset = 0 if (from_start or not path or not os.path.exists(path)) \
            else os.path.getsize(path)
        self.partial = ""
        self.grew_at = time.time()

    def read_all(self) -> list[str]:
        """Every line so far, without moving the tail (for catching up)."""
        if not self.path or not os.path.exists(self.path):
            return []
        return [strip_ansi(l) for l in open(self.path, errors="ignore").read().splitlines()]

    def read(self) -> list[str]:
        if not self.path or not os.path.exists(self.path):
            return []
        size = os.path.getsize(self.path)
        if size < self.offset:
            self.offset, self.partial = 0, ""
        if size == self.offset:
            return []
        with open(self.path, errors="ignore") as f:
            f.seek(self.offset)
            chunk = f.read()
        self.offset = size
        self.grew_at = time.time()
        text = self.partial + chunk
        lines = text.split("\n")
        self.partial = lines.pop()
        return [strip_ansi(l) for l in lines]


def results_dir(root: str) -> str | None:
    """The CURRENT sweep's results dir — None once that sweep has finished.
    A monitor launched alongside a new sweep can start before sweep.sh writes
    its `results=` line; reading the finished sweep's dir then replayed a day
    of old passes as "earlier in this pass" and counted their failures toward
    the no-merge alert (2026-09-30)."""
    p = os.path.join(root, "logs", "sweep.log")
    if not os.path.exists(p):
        return None
    found = None
    for line in open(p, errors="ignore"):
        m = re.search(r"results=(\S+)", line)
        if m:
            found = m.group(1)
        elif any(k in line for k in FINISH_MARKERS):
            found = None
    return found


def newest_console(root: str) -> str | None:
    rd = results_dir(root)
    if rd and os.path.isdir(rd):
        logs = [os.path.join(rd, f) for f in os.listdir(rd) if f.startswith("cycle")]
        if logs:
            return max(logs, key=os.path.getmtime)
        return None                       # sweep started, first pass log not yet written
    sup = os.path.join(root, "logs", "supervisor.log")
    if not os.path.exists(sup):
        return None
    sweep = os.path.join(root, "logs", "sweep.log")
    if os.path.exists(sweep) and os.path.getmtime(sweep) >= os.path.getmtime(sup):
        return None                       # between sweeps: the supervisor log is the old sweep's
    return sup


# ─── Processes and git ────────────────────────────────────────────────────────

def mentions_project(args: str, real_root: str) -> bool:
    """True when any path argument resolves to the project. Compared by real
    path: ~/Code and /Volumes/Lexar/Code can be the same directory, and a
    text match would call a live run dead — and --restart would then launch
    a second sweep on top of it."""
    for tok in args.split():
        if tok.startswith(("/", "~")):
            if os.path.realpath(os.path.expanduser(tok)) == real_root:
                return True
    return False


def run_pids(root: str) -> list[int]:
    """The run's own processes, matched by the script each actually runs
    (sov/procs.py). A shell or editor that mentions work.py and the path is
    not a run process — and must never be killed as one."""
    from sov.procs import mentions, script_of
    real_root = os.path.realpath(root)
    out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        script, rest = script_of(args)
        if script in ("sweep.sh", "supervisor.sh", "work.py") and mentions(rest, real_root):
            pids.append(int(pid))
    return pids


def git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def stale_tree(root: str) -> list[str]:
    wts = [l.split(" ", 1)[1] for l in git(root, "worktree", "list", "--porcelain").stdout.splitlines()
           if l.startswith("worktree ") and "xanadu-worktrees" in l]
    branches = [b for b in git(root, "for-each-ref", "--format=%(refname:short)",
                               "refs/heads/task-*").stdout.split() if b]
    return [f"worktree {w}" for w in wts] + [f"branch {b}" for b in branches]


def dirty_main(root: str) -> list[str]:
    st = git(root, "status", "--porcelain").stdout.splitlines()
    return [l for l in st if l and not l[3:].startswith("logs/")]


def launch(cmd: list[str], cwd: str, env: dict, stdout) -> None:
    """Start the sweep detached from this monitor (survives it exiting)."""
    subprocess.Popen(cmd, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                     start_new_session=True)


# ─── Notifications ────────────────────────────────────────────────────────────

class Notifier:
    def __init__(self, project: str, mac: bool, ntfy_topic: str | None):
        self.title = f"Xanadu · {project}"
        self.mac = mac and sys.platform == "darwin"
        self.ntfy = ntfy_topic

    def send(self, msg: str, urgent: bool = True) -> None:
        if self.mac:
            script = f"display notification {json.dumps(msg)} with title {json.dumps(self.title)}"
            subprocess.run(["osascript", "-e", script], capture_output=True)
        if self.ntfy:
            try:
                req = urllib.request.Request(
                    f"https://ntfy.sh/{self.ntfy}", data=msg.encode(),
                    headers={"Title": self.title, "Priority": "high" if urgent else "default"})
                urllib.request.urlopen(req, timeout=10).close()
            except Exception as exc:  # never let a notification kill the monitor
                print(f"{DIM}(ntfy failed: {exc}){RESET}", flush=True)


# ─── The monitor ──────────────────────────────────────────────────────────────

@dataclass
class Worker:
    task: str = ""
    target: str | None = None
    attempt_at: float | None = None       # an attempt line started, no result yet
    errors: list[str] = field(default_factory=list)   # signatures, current task
    blocked: int = 0
    foreign: int = 0
    last_error: str = ""
    skip_streak: int = 0
    skip_errors: list[str] = field(default_factory=list)


class Monitor:
    def __init__(self, root: str, args, notifier: Notifier, clock=time.time):
        self.root, self.args, self.notifier, self.clock = root, args, notifier, clock
        self.workers: dict[str, Worker] = {}
        self.fired: set[str] = set()
        self.attempts_since_merge = 0
        self.blind_recent: list[bool] = []
        self.finished = False
        self.pass_label = ""
        self.dead_polls = 0
        self.restarts: list[float] = []
        self.gave_up = False
        self.console = Tail(newest_console(root))
        self.sweep = Tail(os.path.join(root, "logs", "sweep.log"))
        self.roadmap_cache = ("", 0.0)
        self.replaying = False
        self.history: list[str] = []
        self.retired_logs: set = set()

    # output ---------------------------------------------------------------
    def stamp(self) -> str:
        return f"{DIM}{time.strftime('%H:%M', time.localtime(self.clock()))}{RESET}"

    def event(self, msg: str) -> None:
        if self.replaying:
            return
        print(f"{self.stamp()}  {msg}", flush=True)

    def alert(self, key: str, msg: str, action: str) -> None:
        if key in self.fired:
            return
        self.fired.add(key)
        if self.replaying:                # history: report once, never notify
            self.history.append(msg)
            return
        print(f"{self.stamp()}  {RED}⚠ {BOLD}{msg}{RESET}\n        {DIM}{action}{RESET}", flush=True)
        self.notifier.send(msg)

    def roadmap(self) -> str:
        p = os.path.join(self.root, "ROADMAP.md")
        try:
            m = os.path.getmtime(p)
        except OSError:
            return ""
        if m != self.roadmap_cache[1]:
            self.roadmap_cache = (open(p, errors="ignore").read(), m)
        return self.roadmap_cache[0]

    # console lines ----------------------------------------------------------
    def on_console(self, raw: str) -> None:
        wid, line = split_worker(raw)
        w = self.workers.setdefault(wid, Worker())
        s = line.strip()

        # "[retry 6] In …" starts a task too: missing it left the previous
        # task's name in place, so a retry's merge was credited to the wrong
        # file and its context checked against the wrong gate (2026-09-30).
        m = re.match(r"\[(?:(\d+)/(\d+)|retry (\d+))\]\s+(.*)", s)
        if m:
            w.task, w.target = m.group(4), task_target(m.group(4))
            w.errors, w.blocked, w.foreign, w.attempt_at = [], 0, 0, None
            where = f"{m.group(1)}/{m.group(2)}" if m.group(1) else f"retry {m.group(3)}"
            self.event(f"{BOLD}▶ {wid} {where}{RESET} {w.target or w.task[:60]}")
            return

        if s.startswith("Files:") and w.target:
            self.check_context(wid, w, s[len("Files:"):])
            return

        if "Attempt" in s and "coding" in s:
            # "timeout — retrying" ends an attempt too: counting on from it
            # measured several attempts as one runaway (2026-09-29).
            done_same_line = any(k in s for k in ("wrote", "no output", "blocked", "timeout",
                                                  "exceeded", "failed"))
            w.attempt_at = None if done_same_line else self.clock()
            if "no output" in s:
                w.last_error = "no output"

        if "blocked bad pattern" in s:
            w.blocked += 1
            if w.blocked >= 4:
                self.alert(f"guard:{wid}:{w.target}",
                           f"{w.blocked} bad-pattern blocks on {w.target} — validation never ran",
                           "A guard may be rejecting correct code; four have. Write the file "
                           "by hand and run it past the guards.")

        if "Validating" in s and "✓" in s:
            w.errors = []
        elif "Validating" in s and "✗" in s:
            msg = s.split("✗", 1)[1].strip()
            sig = error_signature(msg)
            w.errors.append(sig)
            w.last_error = sig
            self.attempts_since_merge += 1
            if len(w.errors) >= 3 and len(set(w.errors[-3:])) == 1:
                self.alert(f"rep:{wid}:{w.target}",
                           f"{w.target}: same error 3 attempts running",
                           f"{sig}\nRepetition means a constant in the environment, not model "
                           "weakness. Check the gate, the guards and the task text.")
            named = {p for p in SRC_PATH.findall(msg) if "test" not in p}
            if w.target and named and w.target not in named \
                    and not any(w.target.endswith(n) or n.endswith(w.target) for n in named):
                w.foreign += 1
                if w.foreign >= 2:
                    self.alert(f"foreign:{wid}:{w.target}",
                               f"{w.target}: errors name {', '.join(sorted(named)[:2])}",
                               "The cause likely landed in an earlier merge. This task may be "
                               "innocent: fix the file the errors name.")
            if self.attempts_since_merge >= self.args.no_merge_after:
                self.alert(f"nomerge:{self.attempts_since_merge // self.args.no_merge_after}",
                           f"{self.attempts_since_merge} failed attempts since anything merged",
                           "Systemic, not per-task. Check the validate command on a clean tree "
                           "and that the task's files aren't locked.")

        if "Marked done" in s:
            self.attempts_since_merge = 0
            self.fired = {k for k in self.fired if not k.startswith("nomerge")}
            w.skip_streak, w.skip_errors, w.attempt_at = 0, [], None
            d, t = counts(self.root)
            self.event(f"{GRN}✓ {wid} {w.target or ''}{RESET}  {GRN}{d}{RESET}/{t}")

        if any(k in s for k in SKIP_MARKERS):
            w.attempt_at = None
            w.skip_streak += 1
            w.skip_errors.append(w.last_error)
            self.event(f"{YEL}⏭ {wid} {w.target or ''}{RESET} {DIM}{w.last_error[:70]}{RESET}")
            if w.skip_streak >= self.args.skip_streak:
                kind, sig = classify_skips(w.skip_errors[-self.args.skip_streak:])
                if kind == "constant":
                    self.alert(f"skips:{wid}:{sig}",
                               f"{wid} skipped {w.skip_streak} tasks in a row, same error",
                               f"{sig}\nA constant cause, not model difficulty. Diagnose before "
                               "spending more attempts.")
                elif w.skip_streak == self.args.skip_streak:
                    where = ("the strong pass should take these" if self.pass_label.endswith("-cheap")
                             else "on the strong model already — check whether these tasks are "
                                  "under-specified or blocked by an earlier merge")
                    self.event(f"{DIM}  {wid}: {w.skip_streak} skips in a row with varied errors — "
                               f"{where}{RESET}")

        if "FATAL" in s or s.startswith("Traceback") or "Unexpected exit code" in s:
            self.alert(f"fatal:{s[:60]}", f"{wid}: {s[:120]}",
                       "Read the lines above it in the cycle log; the first error is the cause.")

    def check_context(self, wid: str, w: Worker, files_part: str) -> None:
        listed = {f.strip() for f in files_part.split(",") if f.strip()}
        wanted = {g for g in gate_files(self.roadmap(), w.target)
                  if os.path.exists(os.path.join(self.root, g))}
        blind = not listed or "(none found)" in files_part or (wanted and not (wanted & listed))
        self.blind_recent = (self.blind_recent + [blind])[-3:]
        if sum(self.blind_recent) >= 2:
            self.alert("blind",
                       "workers are coding without their tests in context",
                       f"{w.target}: gate needs {', '.join(sorted(wanted)) or 'project files'}, "
                       f"context had {len(listed)} file(s). File discovery is broken — check "
                       "where the worker's checkout lives and what all_source_files() skips.")

    # sweep.log --------------------------------------------------------------
    def on_sweep(self, line: str) -> None:
        if "sweep starting" in line:           # sweep.log keeps every sweep; each start is a clean slate
            self.finished = False
            self.fired.discard("finished")
        m = re.search(r"(cycle\d+-\w+)\s+profile=(\S+).*workers=(\d+)", line)
        if m:
            self.pass_label = m.group(1)
            if not self.replaying:
                # The new pass's log doesn't exist yet at this moment, so the
                # newest log is still the OLD pass's. Re-opening it replayed a
                # killed pass — including its unfinished attempt line, which
                # then read as a 25-minute runaway (2026-09-29). Retire the old
                # log and wait for a genuinely new one (see poll()).
                self.retired_logs.add(self.console.path)
                self.console = Tail(None)
                self.workers.clear()
            self.event(f"{BOLD}── {m.group(1)}{RESET} {DIM}{m.group(2)}, {m.group(3)} worker(s){RESET}")
        elif "cap reached" in line:
            self.event(f"{DIM}{line.split('] ', 1)[-1]}{RESET}")
        if any(k in line for k in FINISH_MARKERS):
            self.finished = True

    # periodic checks --------------------------------------------------------
    def periodic(self) -> None:
        now = self.clock()
        for wid, w in self.workers.items():
            if w.attempt_at and (now - w.attempt_at) / 60 >= self.args.runaway_after:
                self.alert(f"runaway:{wid}:{w.target}:{int(w.attempt_at)}",
                           f"{wid}: one attempt on {w.target} running "
                           f"{(now - w.attempt_at) / 60:.0f} min",
                           "A model that won't stop generating. It ends at the profile's "
                           "call_timeout_s; if that's unset, set it (e.g. 900).")

        ph = phantom_done(self.root)
        if ph:
            self.alert("phantom", f"{len(ph)} task(s) ticked done with no file: {', '.join(ph[:3])}",
                       "The merge discarded them — usually a locked_prefix in the active "
                       "config. Nothing after this is trustworthy.")

        alive = bool(run_pids(self.root))
        quiet = (now - max(self.console.grew_at, self.sweep.grew_at)) / 60
        if alive and quiet >= self.args.stall_after:
            self.alert(f"stall:{int(self.console.grew_at)}",
                       f"no log output for {quiet:.0f} min",
                       "Wedged process or model server. Check `ollama ps` and the worker's "
                       "CPU; the harness watchdog exits at 45 min.")

        if alive:
            self.dead_polls = 0
            return
        self.dead_polls += 1
        if self.dead_polls < 2:
            return                              # debounce a pass boundary
        done, total = counts(self.root)
        if self.finished or done >= total:
            if "finished" not in self.fired:
                self.fired.add("finished")
                msg = f"sweep finished: {done}/{total} done, {total - done} open"
                self.event(f"{GRN}{BOLD}{msg}{RESET}")
                self.notifier.send(msg, urgent=False)
            leftovers = stale_tree(self.root)
            if leftovers:
                self.alert("stale", f"{len(leftovers)} leftover worktree(s)/branch(es) after the run",
                           ", ".join(leftovers[:4]) + ". Remove them before the next run.")
            return
        self.stopped(done, total)

    def stopped(self, done: int, total: int) -> None:
        status_p = os.path.join(self.root, "logs", "supervisor.status")
        status = open(status_p).read().strip() if os.path.exists(status_p) else ""
        if status in ("needs_fix", "stuck"):
            self.alert(f"parked:{status}", f"run parked for a human ({status}) at {done}/{total}",
                       "See logs/escalate*.md for what it needs.")
            return
        if not self.args.restart or self.gave_up:
            self.alert("stopped", f"run stopped unexpectedly at {done}/{total}",
                       "No sweep/supervisor/work.py processes and no clean finish logged. "
                       "Check the end of the cycle log and sweep-console.log.")
            return
        recent = [t for t in self.restarts if self.clock() - t < 3600]
        if len(recent) >= self.args.max_restarts:
            self.gave_up = True
            self.alert("gaveup", f"run keeps dying — {len(recent)} restarts in the last hour; "
                       "stopped restarting", "It is failing for a reason a restart won't fix. "
                       "Read the last cycle log.")
            return
        self.restart(done, total)

    def restart(self, done: int, total: int) -> None:
        env = dict(kv.split("=", 1) for kv in shlex.split(self.args.restart))
        main = env.get("MAIN_BRANCH", "main")
        for pid in run_pids(self.root):
            os.kill(pid, signal.SIGKILL)
        time.sleep(2)
        if os.path.exists(os.path.join(self.root, ".git", "MERGE_HEAD")):
            git(self.root, "merge", "--abort")
        for dirpath, _, files in os.walk(os.path.join(self.root, ".git")):
            for f in files:
                if f.endswith(".lock"):
                    os.remove(os.path.join(dirpath, f))
        dirty = dirty_main(self.root)
        if dirty or git(self.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != main:
            self.gave_up = True
            self.alert("restart-refused", f"run stopped at {done}/{total}; NOT restarting — "
                       "the main checkout has uncommitted changes or is off "
                       f"'{main}'", "\n".join(dirty[:6]) + "\nThat looks like a human edit, "
                       "never debris. Commit or discard it, then relaunch.")
            return
        for item in stale_tree(self.root):
            kind, ref = item.split(" ", 1)
            if kind == "worktree":
                git(self.root, "worktree", "remove", "--force", ref)
        git(self.root, "worktree", "prune")
        for item in stale_tree(self.root):
            kind, ref = item.split(" ", 1)
            if kind == "branch":
                git(self.root, "branch", "-D", ref)
        console = open(os.path.join(self.root, "logs", "sweep-console.log"), "a")
        launch(["./sweep.sh", self.root], cwd=SOVEREIGN, env={**os.environ, **env},
               stdout=console)
        self.restarts.append(self.clock())
        self.dead_polls = 0
        self.finished = False
        self.fired = {k for k in self.fired if k not in ("stopped",)}
        msg = f"run had stopped at {done}/{total} — restarted it (restart {len(self.restarts)})"
        self.event(f"{YEL}{BOLD}↻ {msg}{RESET}")
        self.notifier.send(msg)

    # loop -------------------------------------------------------------------
    def poll(self) -> None:
        for line in self.sweep.read():
            self.on_sweep(line)
        newest = newest_console(self.root)
        if newest and newest != self.console.path and newest not in self.retired_logs:
            self.console = Tail(newest, from_start=True)
        for line in self.console.read():
            self.on_console(line)
        # an attempt whose result hasn't been printed yet shows as a partial line
        wid, part = split_worker(strip_ansi(self.console.partial))
        if "Attempt" in part and "coding" in part:
            w = self.workers.setdefault(wid, Worker())
            w.attempt_at = w.attempt_at or self.clock()
        self.periodic()

    def catch_up(self) -> None:
        """Replay the current pass's log silently to learn each worker's task
        and any attempt in flight; list (not notify) anything already wrong."""
        self.replaying = True
        self.console = Tail(self.console.path, from_start=True)
        for line in self.sweep.read_all():
            self.on_sweep(line)
        for line in self.console.read():
            self.on_console(line)
        wid, part = split_worker(strip_ansi(self.console.partial))
        if "Attempt" in part and "coding" in part:
            self.workers.setdefault(wid, Worker()).attempt_at = self.clock()
        self.replaying = False
        for msg in self.history:
            print(f"{DIM}earlier in this pass:{RESET} {YEL}{msg}{RESET}", flush=True)

    def snapshot(self) -> str:
        done, total = counts(self.root)
        alive = "running" if run_pids(self.root) else "no run processes"
        parts = [f"{done}/{total} done", alive]
        if self.pass_label:
            parts.append(self.pass_label)
        for wid, w in sorted(self.workers.items()):
            if w.target:
                parts.append(f"{wid}: {w.target}")
        return " · ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("--every", type=int, default=20, help="poll interval, seconds")
    ap.add_argument("--once", action="store_true", help="one snapshot and check, then exit")
    ap.add_argument("--ntfy-topic", default=os.getenv("NTFY_TOPIC"),
                    help="also push alerts to https://ntfy.sh/<topic> (pick an unguessable topic)")
    ap.add_argument("--no-mac", action="store_true", help="skip macOS notifications")
    ap.add_argument("--restart", default=None, metavar="ENV",
                    help='restart a run that dies, e.g. "MAIN_BRANCH=main CHEAP=qwen25-t1 '
                         'STRONG=qwen38 CHEAP_WORKERS=2 STRONG_WORKERS=1"')
    ap.add_argument("--max-restarts", type=int, default=2, help="per rolling hour")
    ap.add_argument("--runaway-after", type=float, default=25, help="minutes for one attempt")
    ap.add_argument("--stall-after", type=float, default=40, help="minutes with no log output")
    ap.add_argument("--skip-streak", type=int, default=4)
    ap.add_argument("--no-merge-after", type=int, default=12, help="failed attempts")
    ap.add_argument("--summary-every", type=float, default=0,
                    help="minutes between one-line status summaries (0 = never)")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.project))
    notifier = Notifier(os.path.basename(root), not args.no_mac, args.ntfy_topic)
    mon = Monitor(root, args, notifier)
    mon.catch_up()
    if args.once:
        mon.notifier = Notifier(os.path.basename(root), False, None)   # a snapshot never pings
        mon.periodic()
        print(mon.snapshot())
        return 0

    print(f"{BOLD}watching {root}{RESET}  {DIM}(events and alerts only — silence means "
          f"nothing needs you){RESET}", flush=True)
    print(f"{DIM}{mon.snapshot()}{RESET}", flush=True)
    last_summary = time.time()
    while True:
        time.sleep(args.every)
        mon.poll()
        if args.summary_every and time.time() - last_summary >= args.summary_every * 60:
            last_summary = time.time()
            print(f"{mon.stamp()}  {DIM}{mon.snapshot()}{RESET}", flush=True)
        if mon.finished and "finished" in mon.fired:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
