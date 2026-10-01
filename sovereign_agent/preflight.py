#!/usr/bin/env python3
"""preflight.py — find kit problems before a worker does.

    python3 preflight.py ~/Code/<project>                    # static checks (seconds, no model)
    python3 preflight.py ~/Code/<project> --solve qwen38     # + dry-run every open task on a profile

WHY (2026-09-30, xmeasure)

Every failure that day was in the kit, not the model, and each was found the
expensive way — one failed attempt at a time, a pass each: a task that called
seventeen functions it never named, an ambiguous import line, a Config whose
module nothing stated, a finished task broken on main by a later task, a
cheat sheet that forbade a property the task needed. A reference solve by
hand on the 15 open tasks found the remaining five such gaps in under an
hour. This makes that a command.

STATIC CHECKS (deterministic)
  done-gates   every finished task's gate passes on main
  size         check_task_size.oversized (incl. calls to unnamed functions)
  coverage     every name a gate's test imports from the project is either
               built by the task or named — with its module — in the task
               text or the always-included context
  order        no task depends on a file only a LATER task creates

DRY RUN (--solve PROFILE)
  Clones the project (never touches it), then runs work.py on each open task
  in ROADMAP order, one at a time, keeping passing solutions so later tasks
  build on them. Each failure gets a likely cause from its log: over-scoped
  (silent timeouts), missing dependency, wrong import, contract gap (unknown
  attribute), guard conflict, foreign file. Run it when no sweep is using
  Ollama.

Writes logs/preflight-<date>.md in the project and prints the summary.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bench import git, live_runs, only_open, task_text  # noqa: E402
from check_task_size import oversized  # noqa: E402
from sov.deps import _missing_python_deps  # noqa: E402
from sov.runctl import broken_done_gates  # noqa: E402

PYTHON = str(HERE / ".venv" / "bin" / "python")
WORK = Path(os.environ.get("XANADU_PREFLIGHT_WORK", Path.home() / ".cache" / "xanadu-preflight"))


# ─── static ──────────────────────────────────────────────────────────────────

def open_tasks(roadmap: str) -> list[str]:
    return [l for l in roadmap.splitlines() if l.startswith("- [ ]")]


def target_of(line: str) -> str | None:
    m = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", line)
    return m.group(1) if m else None


def gate_of(line: str) -> str | None:
    m = re.search(r"task gate:\s*(.+?)\s+passes\s*$", line.strip())
    return m.group(1) if m else None


def context_files(root: str) -> list[str]:
    try:
        return list(json.load(open(os.path.join(root, ".sovereign_config.json")))
                    .get("context_always_include", []))
    except (OSError, ValueError):
        return []


def context_text(root: str) -> str:
    """The always-included context every worker prompt carries."""
    try:
        cfg = json.load(open(os.path.join(root, ".sovereign_config.json")))
    except (OSError, ValueError):
        cfg = {}
    out = []
    for rel in cfg.get("context_always_include", []):
        try:
            out.append(open(os.path.join(root, rel), errors="ignore").read())
        except OSError:
            pass
    return "\n".join(out)


def coverage_gaps(line: str, root: str, ctx: str) -> list[str]:
    """Names the gate's test imports from the project that the worker is never
    told where to find."""
    target, gate = target_of(line), gate_of(line)
    if not target or not gate or not target.endswith(".py"):
        return []
    pkg = target.split("/")[0]
    own_mod = target[:-3].replace("/", ".")
    gaps = []
    for tf in sorted(set(re.findall(r"([\w./-]+\.py)", gate))):
        try:
            src = open(os.path.join(root, tf), errors="ignore").read()
        except OSError:
            continue
        for mod, names in re.findall(rf"^\s*from\s+({re.escape(pkg)}[\w.]*)\s+import\s+([\w, ]+)", src, re.M):
            if mod == own_mod:
                continue
            for name in (n.strip() for n in names.split(",") if n.strip()):
                path = mod.replace(".", "/") + ".py"
                in_task = name in line and (mod in line or path in line)
                # an always-included source file shows its own names (constants.py, model.py)
                in_file = path in context_files(root) and \
                    re.search(rf"^(?:def |class )?{re.escape(name)}\b", open(os.path.join(root, path),
                              errors="ignore").read(), re.M)
                in_ctx = re.search(rf"#\s*{re.escape(path)}\b[\s\S]*?\b{re.escape(name)}\b", ctx) or \
                    f"from {mod} import" in ctx and name in ctx
                if not (in_task or in_ctx or in_file):
                    gaps.append(f"{tf} imports `{name}` from `{mod}`, which neither the task text "
                                f"nor the always-included context names")
    return gaps


def order_problems(tasks: list[str], root: str) -> list[str]:
    """A task needing a file that only a LATER open task creates."""
    later_targets: dict[str, int] = {}
    for i, l in enumerate(tasks):
        t = target_of(l)
        if t:
            later_targets.setdefault(t, i)
    out = []
    for i, l in enumerate(tasks):
        for dep in _missing_python_deps(l[6:], root):
            j = later_targets.get(dep)
            if j is not None and j > i:
                out.append(f"{target_of(l)} needs {dep}, which a later task (#{j + 1}) creates — reorder")
            elif j is None and dep.endswith(".py"):
                out.append(f"{target_of(l)} needs {dep}, which no open task creates")
    return out


def static_checks(root: str) -> dict:
    roadmap = open(os.path.join(root, "ROADMAP.md"), errors="ignore").read()
    tasks = open_tasks(roadmap)
    ctx = context_text(root)
    if any(root in l for l in live_runs()):
        broken, complete = [], False          # a live run is switching branches under us
    else:
        broken, complete = broken_done_gates(root)
    return {
        "open": len(tasks),
        "done_gates_broken": broken,
        "done_gates_complete": complete,
        "oversized": [f.message for f in oversized(roadmap)],
        "coverage": {target_of(l): g for l in tasks if (g := coverage_gaps(l, root, ctx))},
        "order": order_problems(tasks, root),
    }


# ─── dry run ─────────────────────────────────────────────────────────────────

CAUSES = [
    ("over-scoped", r"two timeouts with no code|chars produced\) — the model is streaming"),
    ("missing dependency", r"Deferred: depends on|ModuleNotFoundError: No module named '([\w.]+)'"),
    ("wrong import", r"ImportError: cannot import name '(\w+)' from '([\w.]+)'"),
    ("contract gap", r"AttributeError: '?(\w+)'? object has no attribute '(\w+)'|"
                     r"has no attribute '(\w+)'|NameError: name '(\w+)' is not defined"),
    ("guard conflict", r"blocked bad pattern"),
    ("foreign file", r"may be innocent|errors name no file this task wrote"),
]


def classify(log: str) -> tuple[str, str]:
    for cause, pat in CAUSES:
        m = list(re.finditer(pat, log))
        if m:
            return cause, m[-1].group(0)[:160]
    return "model", ""


def dry_run(root: str, profile: str, timeout: int, log=print) -> list[dict]:
    wd = WORK / os.path.basename(root)
    shutil.rmtree(wd, ignore_errors=True)
    wd.parent.mkdir(parents=True, exist_ok=True)
    git(wd.parent, "clone", "-q", root, str(wd))
    git(wd, "remote", "remove", "origin", check=False)       # never push anywhere
    for link in (".venv", "node_modules", ".dart_tool"):
        if (Path(root) / link).exists() and not (wd / link).exists():
            (wd / link).symlink_to(Path(root) / link)
            with open(wd / ".git" / "info" / "exclude", "a") as f:
                f.write(f"\n/{link}\n")
    full = (wd / "ROADMAP.md").read_text()
    env = {**os.environ, "SOVEREIGN_PROFILE": profile, "FIRESTORE_PROJECT_ID": "",
           "MAIN_BRANCH": git(wd, "rev-parse", "--abbrev-ref", "HEAD").strip()}
    results, solved = [], set()
    for i, line in enumerate(open_tasks(full), 1):
        target = target_of(line)
        log(f"[{i}] {target} …")
        (wd / "ROADMAP.md").write_text(only_open((wd / "ROADMAP.md").read_text(), line))
        git(wd, "commit", "-qam", f"preflight: only {target} open", check=False)
        t0 = time.time()
        out = wd / "logs" / f"preflight-{i}.log"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w") as f:
            try:
                subprocess.run([PYTHON, "-u", str(HERE / "work.py"), "--project", str(wd), "--no-retry"],
                               cwd=HERE, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                f.write("\n(preflight: task timeout)\n")
        text = out.read_text(errors="ignore")
        passed = ("- [x] " + task_text(line)) in (wd / "ROADMAP.md").read_text()
        cause, evidence = ("", "") if passed else classify(text)
        results.append({"n": i, "target": target, "passed": passed, "cause": cause,
                        "evidence": evidence, "minutes": round((time.time() - t0) / 60, 1)})
        log(f"    {'PASS' if passed else 'fail — ' + cause} ({results[-1]['minutes']} min)")
        if passed:
            solved.add(line[6:])
        # reopen what only_open ticked: originally-open tasks the dry run hasn't solved
        orig_open = {l[6:] for l in open_tasks(full)}
        restored = ["- [ ] " + l[6:] if l.startswith("- [x] ") and l[6:] in orig_open
                    and l[6:] not in solved else l
                    for l in (wd / "ROADMAP.md").read_text().splitlines()]
        (wd / "ROADMAP.md").write_text("\n".join(restored) + "\n")
        git(wd, "commit", "-qam", "preflight: restore open tasks", check=False)
    return results


# ─── report ──────────────────────────────────────────────────────────────────

def report(root: str, st: dict, runs: list[dict] | None) -> str:
    lines = [f"# Preflight — {os.path.basename(root)} — {datetime.now():%Y-%m-%d %H:%M}", "",
             f"{st['open']} open task(s).", ""]
    def section(title, items):
        lines.append(f"## {title} — {'OK' if not items else len(items)}")
        lines.extend(f"- {x}" for x in items)
        lines.append("")
    section("Finished tasks failing their gate on main", st["done_gates_broken"])
    section("Oversized tasks", st["oversized"])
    section("Context coverage", [f"{t}: {g}" for t, gs in st["coverage"].items() for g in gs])
    section("Ordering", st["order"])
    if runs is not None:
        ok = sum(r["passed"] for r in runs)
        lines += [f"## Dry run — {ok}/{len(runs)} passed", ""]
        for r in runs:
            lines.append(f"- {'PASS' if r['passed'] else 'FAIL'} {r['target']} ({r['minutes']} min)"
                         + ("" if r["passed"] else f" — **{r['cause']}**: `{r['evidence']}`"))
        lines.append("")
    if not st["done_gates_complete"]:
        lines.append("_Done-gate check skipped (a run is live on this project) or stopped at its time budget._")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("--solve", metavar="PROFILE", help="dry-run every open task on this profile")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds per task in the dry run")
    ap.add_argument("--force", action="store_true", help="dry-run even while a run uses Ollama")
    a = ap.parse_args()
    root = os.path.realpath(os.path.expanduser(a.project))
    st = static_checks(root)
    runs = None
    if a.solve:
        live = live_runs()
        if live and not a.force:
            print("refusing the dry run: a run is using Ollama (stop it, or pass --force)")
            return 1
        runs = dry_run(root, a.solve, a.timeout)
    text = report(root, st, runs)
    out = Path(root) / "logs" / f"preflight-{datetime.now():%Y%m%d-%H%M}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text + "\n")
    print(text)
    print(f"\n→ {out}")
    problems = st["done_gates_broken"] or st["oversized"] or st["coverage"] or st["order"] \
        or (runs and not all(r["passed"] for r in runs))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
