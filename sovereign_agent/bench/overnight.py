#!/usr/bin/env python3
"""overnight.py — run a night's queue of Xanadu work in order, unattended.

    python3 bench/overnight.py --project ~/Code/xmeasure --suite xmeasure-v1 \\
        --sweep-env "MAIN_BRANCH=main CHEAP=qwen25-t1 STRONG=qwen38 CHEAP_WORKERS=2 STRONG_WORKERS=1" \\
        --until 08:00

Ollama serves one run at a time, so the night is a sequence:

  1. pause   stop the project's monitor and sweep, park the repo cleanly
  2. probe   one tiny request to the strong model, thinking default vs off —
             is the switch accepted, and is thinking where the time goes?
  3. A/B     benchmark hard cases on the strong profile, thinking default vs off
  4. sweep   relaunch sweep + monitor (--restart), thinking off if it won;
             wait for the sweep to finish (never kill it)
  5. base    full-suite baseline on the cheap profile (reference for later)
  6. rag     RAG on vs off on the hard cases, cheap profile
  7. mine    api_surface.py --mine report (no --apply) for the morning

Steps 5-7 start only before --until; step 4 is allowed to run past it. Every
step's outcome lands in bench/results/overnight-<date>.md and as a
notification; a failed step is recorded and the queue moves on.
"""

import argparse
import json
import os
import shlex
import signal
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import bench      # noqa: E402
import monitor    # noqa: E402

PY = str(HERE / ".venv" / "bin" / "python")


class Night:
    def __init__(self, a):
        self.a = a
        self.root = str(Path(a.project).expanduser().resolve())
        self.started = datetime.now()
        hh, mm = map(int, a.until.split(":"))
        self.deadline = self.started.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if self.deadline <= self.started:
            self.deadline += timedelta(days=1)
        bench.RESULTS.mkdir(parents=True, exist_ok=True)
        self.report = bench.RESULTS / f"overnight-{self.started:%Y%m%d-%H%M}.md"
        self.notifier = monitor.Notifier("overnight", not a.no_mac, a.ntfy_topic)
        self.env = dict(kv.split("=", 1) for kv in shlex.split(a.sweep_env))
        self.strong = self.env.get("STRONG", "qwen38")
        self.cheap = self.env.get("CHEAP", "qwen25-t1")
        self.write(f"# Overnight queue — {self.started:%Y-%m-%d %H:%M}\n\n"
                   f"Project `{self.root}`, suite `{a.suite}`, new work stops at {a.until}.\n")

    # reporting ---------------------------------------------------------------
    def write(self, text: str) -> None:
        with open(self.report, "a") as f:
            f.write(text + "\n")

    def say(self, text: str, notify: bool = False) -> None:
        line = f"[{datetime.now():%H:%M}] {text}"
        print(line, flush=True)
        self.write(f"- {line}")
        if notify:
            self.notifier.send(text, urgent=False)

    def time_left(self) -> bool:
        return datetime.now() < self.deadline

    # 1. pause ----------------------------------------------------------------
    def pause(self) -> None:
        real = os.path.realpath(self.root)
        out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            pid, _, args = line.strip().partition(" ")
            if "monitor.py" in args and monitor.mentions_project(args, real):
                os.kill(int(pid), signal.SIGTERM)       # first, or it restarts the sweep
        time.sleep(1)
        for pid in monitor.run_pids(self.root):
            os.kill(pid, signal.SIGKILL)
        time.sleep(3)
        if monitor.run_pids(self.root):
            raise RuntimeError("run processes survived the kill")
        root = self.root
        if os.path.exists(os.path.join(root, ".git", "MERGE_HEAD")):
            monitor.git(root, "merge", "--abort")
        for dirpath, _, files in os.walk(os.path.join(root, ".git")):
            for f in files:
                if f.endswith(".lock"):
                    os.remove(os.path.join(dirpath, f))
        for item in monitor.stale_tree(root):
            kind, ref = item.split(" ", 1)
            if kind == "worktree":
                monitor.git(root, "worktree", "remove", "--force", ref)
        monitor.git(root, "worktree", "prune")
        main = self.env.get("MAIN_BRANCH", "main")
        monitor.git(root, "checkout", "-q", "-f", main)
        for item in monitor.stale_tree(root):
            kind, ref = item.split(" ", 1)
            if kind == "branch":
                monitor.git(root, "branch", "-D", ref)
        dirty = monitor.dirty_main(root)
        if dirty:
            raise RuntimeError(f"main checkout not clean after pause: {dirty[:5]}")
        d, t = monitor.counts(root)
        self.say(f"paused the sweep at {d}/{t}; repo parked clean")

    # 2. probe ----------------------------------------------------------------
    def probe(self) -> bool | None:
        """Returns True if thinking looks like the time sink, False if not,
        None if think=false is not accepted by the model."""
        model = self.strong_model()
        self.write(f"\n## Thinking probe ({model})\n")

        def ask(extra):
            body = {"model": model, "stream": True,
                    "messages": [{"role": "user", "content":
                                  "Write a Python function add(a, b) that returns a + b. Code only."}],
                    **extra}
            req = urllib.request.Request("http://localhost:11434/api/chat",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            t0, content, thinking = time.time(), 0, 0
            with urllib.request.urlopen(req, timeout=900) as r:
                for line in r:
                    m = json.loads(line).get("message", {})
                    content += len(m.get("content") or "")
                    thinking += len(m.get("thinking") or "")
            return round(time.time() - t0, 1), content, thinking

        try:
            dflt = ask({})
        except Exception as exc:
            self.say(f"probe (default) failed: {exc}")
            return None
        self.say(f"thinking default: {dflt[0]}s, {dflt[1]} answer chars, {dflt[2]} thinking chars")
        try:
            off = ask({"think": False})
        except urllib.error.HTTPError as exc:
            self.say(f"model rejected think=false (HTTP {exc.code}) — skipping the A/B", notify=True)
            return None
        except Exception as exc:
            self.say(f"probe (think off) failed: {exc}")
            return None
        self.say(f"thinking off:     {off[0]}s, {off[1]} answer chars, {off[2]} thinking chars")
        return dflt[2] > 0

    def strong_model(self) -> str:
        r = subprocess.run([PY, "-c", "import sov.config as c; print(c.TIER1_MODEL)"], cwd=HERE,
                           env={**os.environ, "SOVEREIGN_PROFILE": self.strong},
                           capture_output=True, text=True)
        return r.stdout.strip().splitlines()[-1]

    # benchmark runs ----------------------------------------------------------
    def bench(self, label: str, profile: str, extra: list[str]) -> Path | None:
        before = set(bench.RESULTS.glob("2*"))
        cmd = [PY, str(HERE / "bench.py"), "run", "--suite", self.a.suite, "--profile", profile,
               "--label", label, "--stop-at", self.a.until, *extra]
        self.say(f"benchmark {label}: {' '.join(extra)}")
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        tail = "\n".join(r.stdout.strip().splitlines()[-3:])
        new = sorted(set(bench.RESULTS.glob("2*")) - before)
        if r.returncode != 0 or not new:
            self.say(f"benchmark {label} failed: {(r.stderr or r.stdout).strip()[-300:]}")
            return None
        self.write(f"\n```\n{tail}\n```")
        return new[-1]

    def compare(self, a: Path, b: Path) -> str:
        r = subprocess.run([PY, str(HERE / "bench.py"), "compare", str(a), str(b)], cwd=HERE,
                           capture_output=True, text=True)
        self.write(f"\n```\n{r.stdout.strip()}\n```")
        return r.stdout

    @staticmethod
    def score(run: Path) -> tuple[int, float]:
        recs = [json.loads(l) for l in (run / "results.jsonl").read_text().splitlines() if l.strip()]
        walls = [r["wall_s"] for r in recs if r["passed"]] or [float("inf")]
        return sum(r["passed"] for r in recs), statistics.median(walls)

    # 4. sweep ----------------------------------------------------------------
    def sweep(self, think_off: bool) -> None:
        env = dict(self.env)
        if think_off:
            env["OLLAMA_THINK"] = "0"
        env_str = " ".join(f"{k}={v}" for k, v in env.items())
        console = open(os.path.join(self.root, "logs", "sweep-console.log"), "a")
        subprocess.Popen(["./sweep.sh", self.root], cwd=HERE, env={**os.environ, **env},
                         stdout=console, stderr=subprocess.STDOUT, start_new_session=True)
        mlog = open(os.path.join(self.root, "logs", "monitor.log"), "a")
        margs = [PY, "-u", str(HERE / "monitor.py"), self.root, "--summary-every", "60",
                 "--restart", env_str]
        if self.a.ntfy_topic:
            margs += ["--ntfy-topic", self.a.ntfy_topic]
        subprocess.Popen(margs, cwd=HERE, stdout=mlog, stderr=subprocess.STDOUT,
                         start_new_session=True)
        self.say(f"sweep relaunched ({env_str}) with monitor", notify=True)
        time.sleep(60)
        while True:
            time.sleep(120)
            sweep_log = Path(self.root, "logs", "sweep.log").read_text(errors="ignore")
            tail = sweep_log[-3000:]
            finished = any(k in tail for k in monitor.FINISH_MARKERS)
            if finished and not monitor.run_pids(self.root):
                d, t = monitor.counts(self.root)
                self.say(f"sweep finished at {d}/{t}", notify=True)
                return
            if datetime.now() >= self.deadline + timedelta(hours=2):
                self.say("sweep still running 2h past the deadline — leaving it running; "
                         "queue ends here", notify=True)
                raise SystemExit(0)

    # 7. mine -----------------------------------------------------------------
    def mine(self) -> None:
        r = subprocess.run([PY, str(HERE / "api_surface.py"), self.root, "--mine"], cwd=HERE,
                           capture_output=True, text=True, timeout=900)
        out = (r.stdout + r.stderr).strip()
        self.write(f"\n## api_surface.py --mine (report only — nothing applied)\n\n```\n{out[-6000:]}\n```")
        self.say(f"guessed-API report written ({len(out.splitlines())} lines)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--suite", required=True)
    ap.add_argument("--sweep-env", required=True)
    ap.add_argument("--until", default="08:00")
    ap.add_argument("--ab-cases", type=int, default=4)
    ap.add_argument("--ab-timeout", type=int, default=1200)
    ap.add_argument("--rag-cases", type=int, default=6)
    ap.add_argument("--ntfy-topic", default=os.getenv("NTFY_TOPIC"))
    ap.add_argument("--no-mac", action="store_true")
    a = ap.parse_args()
    n = Night(a)

    def step(name, fn, *args):
        try:
            return fn(*args)
        except SystemExit:
            raise
        except Exception as exc:
            n.say(f"step '{name}' failed: {exc} — continuing", notify=True)
            return None

    if step("pause", n.pause) is None and monitor.run_pids(n.root):
        n.say("could not pause the sweep — stopping the queue", notify=True)
        return 1

    think_off = False
    thinking = step("probe", n.probe)
    if thinking is not None:
        n.write("\n## Thinking A/B (hard cases, strong profile)\n")
        common = ["--tag", "hard", "--limit", str(a.ab_cases), "--timeout", str(a.ab_timeout)]
        on = step("ab-default", n.bench, "think-default", n.strong, common)
        off = step("ab-off", n.bench, "think-off", n.strong, common + ["--set", "OLLAMA_THINK=0"])
        if on and off:
            n.compare(on, off)
            (p_on, m_on), (p_off, m_off) = n.score(on), n.score(off)
            think_off = p_off > p_on or (p_off == p_on and m_off < m_on)
            n.say(f"A/B: default {p_on} passed (median {m_on:.0f}s), off {p_off} passed "
                  f"(median {m_off:.0f}s) → sweep runs with thinking "
                  f"{'OFF' if think_off else 'as before'}", notify=True)

    n.write("\n## Sweep\n")
    step("sweep", n.sweep, think_off)

    if n.time_left():
        n.write("\n## Baseline (full suite, cheap profile)\n")
        extra = ["--timeout", "900"] + (["--set", "OLLAMA_THINK=0"] if think_off else [])
        base = step("baseline", n.bench, "baseline", n.cheap, extra)
        if base and n.time_left():
            n.write("\n## RAG on vs off (hard cases, cheap profile)\n")
            rag = step("rag", n.bench, "rag-on", n.cheap,
                       extra + ["--tag", "hard", "--limit", str(a.rag_cases),
                                "--config", "rag_enabled=true"])
            if rag:
                n.compare(base, rag)
    else:
        n.say("past the deadline — skipping baseline and RAG runs")

    step("mine", n.mine)
    n.say(f"queue done — report: {n.report}", notify=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
