#!/usr/bin/env python3
"""bench.py — a fixed benchmark for Xanadu, so harness and model changes are
measured instead of guessed.

    # build a suite from a project's history (every merged task = a case)
    python3 bench.py make --project ~/Code/xmeasure --name xmeasure-v1

    # run it with a profile (and any env overrides), then compare runs
    python3 bench.py run --suite xmeasure-v1 --profile qwen38 --label think-on
    python3 bench.py run --suite xmeasure-v1 --profile qwen38 --set OLLAMA_THINK=0 --label think-off
    python3 bench.py compare bench/results/<run A> bench/results/<run B>

    # the same cases through Aider (same model, same gate) — engine vs engine
    python3 bench.py run --suite xmeasure-v1 --profile qwen38 --engine aider --label aider-qwen38

WHY

On 2026-09-28 the harness changed eight times in one day (worktrees, a
5,000-line refactor, timeouts) with nothing to say whether each change made
runs better or worse, and "is qwen3.8 slow because it thinks?" could only be
tested by experimenting on a live run. A benchmark makes each of those a
table.

HOW A CASE WORKS

A case is a task that really merged: the snapshot is main just before its
merge, so the task is open and everything it depends on is already there;
its gate is known to pass (make --verify checks it at the merge commit).
Each run clones the project at that snapshot, leaves only that task open,
runs work.py once (no retry pass), then runs the task's gate ITSELF — the
harness's own "done" is recorded but never trusted on its own.

Runs refuse to start while any sweep/supervisor/work.py is running: they
would share Ollama with it and every timing would be meaningless.
"""

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from sov.offline import no_pub  # noqa: E402

HERE = Path(__file__).resolve().parent
SUITES = HERE / "bench" / "suites"
RESULTS = HERE / "bench" / "results"
WORK = Path(os.environ.get("XANADU_BENCH_WORK",
                           Path.home() / ".cache" / "xanadu-bench" / "work"))
PYTHON = str(HERE / ".venv" / "bin" / "python")
GATE_RE = re.compile(r"task gate:\s*(.+?)(?:\s+passes)?\s*$", re.I)


# ─── Pure helpers (unit-tested) ───────────────────────────────────────────────

def task_text(line: str) -> str:
    return re.sub(r"^- \[[ x]\]\s*", "", line.strip())


def extract_gate(line: str) -> str | None:
    m = GATE_RE.search(line)
    return m.group(1).strip() if m else None


def target_of(text: str) -> str | None:
    m = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", text)
    return m.group(1) if m else None


def find_task_line(roadmap: str, prefix: str) -> str | None:
    """The unchecked ROADMAP line whose task text starts with `prefix` (a merge
    commit carries only the first 72 characters of the task)."""
    prefix = prefix.strip()
    hits = [l for l in roadmap.splitlines()
            if l.startswith("- [ ] ") and task_text(l).startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


def only_open(roadmap: str, keep_line: str) -> str:
    """ROADMAP with `keep_line` as the only open task. Other open tasks are
    DROPPED, not ticked: a ticked future task reads as finished, and the
    same-file regression check then runs its gate against a function nobody
    has written (2026-10-01: both format.py tasks failed each other's gate in
    a preflight dry run)."""
    out = []
    for line in roadmap.splitlines(keepends=True):
        if line.startswith("- [ ] ") and line.rstrip("\n") != keep_line:
            continue
        out.append(line)
    return "".join(out)


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-")


def spread(cases: list[dict], n: int) -> list[dict]:
    """n cases spread across as many different target files as possible:
    round-robin over targets in suite order (deterministic). "The first 4
    hard cases" were once all timeparse.py functions."""
    groups: dict[str, list[dict]] = {}
    for c in cases:
        groups.setdefault(c.get("target") or c["id"], []).append(c)
    out, depth = [], 0
    while len(out) < n and any(len(g) > depth for g in groups.values()):
        for g in groups.values():
            if len(g) > depth and len(out) < n:
                out.append(g[depth])
        depth += 1
    return out


# Settings that only act on attempt 2+: an arm using one can't have made a
# difference when no case in it ever retried.
RETRY_ONLY_SETTINGS = {"OLLAMA_THINK_RETRIES"}


def pick_winner(arms: list[tuple[str, dict, list[dict]]], min_gain: float = 0.20) -> tuple[str, dict, str]:
    """Choose between benchmark arms without chasing noise.

    arms: [(label, env, records)], the FIRST being the baseline (current
    settings). An arm beats the baseline only by passing more cases, or by
    passing as many while being at least `min_gain` faster on the median AND
    faster on at least two-thirds of the cases both passed. An arm whose
    settings can't have taken effect is never chosen. Returns (label, env, why).

    2026-09-29: the old rule (most passes, then lowest median) picked a
    retry-only setting on a 12% median difference when no case had retried.
    """
    def passed(recs):
        return {r["case"]: r for r in recs if r["passed"]}

    def median(vals):
        return statistics.median(vals) if vals else float("inf")

    base_label, base_env, base_recs = arms[0]
    base = passed(base_recs)
    best, why = (base_label, base_env), "no arm beat the baseline clearly — keeping current settings"
    best_key = (len(base), 0.0)
    for label, env, recs in arms[1:]:
        if set(env) & RETRY_ONLY_SETTINGS and not any((r.get("attempts") or 0) > 1 for r in recs):
            continue                                  # its setting never engaged
        got = passed(recs)
        if len(got) > best_key[0]:
            best, best_key = (label, env), (len(got), 0.0)
            why = f"{label} passed {len(got)} vs {len(base)}"
            continue
        if len(got) != len(base) or len(got) < best_key[0]:
            continue
        shared = [c for c in got if c in base]
        if not shared:
            continue
        m_arm, m_base = median([got[c]["wall_s"] for c in shared]), median([base[c]["wall_s"] for c in shared])
        faster = sum(got[c]["wall_s"] < base[c]["wall_s"] for c in shared)
        gain = 1 - m_arm / m_base
        if gain >= min_gain and faster * 3 >= len(shared) * 2 and gain > best_key[1]:
            best, best_key = (label, env), (len(got), gain)
            why = f"{label} {gain:.0%} faster on the median, faster on {faster}/{len(shared)} cases"
    return best[0], best[1], why


def summarize(records: list[dict]) -> dict:
    passed = [r for r in records if r["passed"]]
    first = [r for r in passed if r.get("attempts") == 1]
    walls = [r["wall_s"] for r in passed]
    return {
        "cases": len(records),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(records), 3) if records else 0.0,
        "first_attempt": len(first),
        "median_wall_s_passed": round(statistics.median(walls), 1) if walls else None,
        "total_wall_s": round(sum(r["wall_s"] for r in records), 1),
        "harness_lied": sum(1 for r in records if r["marked"] and not r["gate_ok"]),
        "edited_protected": sum(1 for r in records if r.get("edited_protected")),
    }


# ─── Processes / git ──────────────────────────────────────────────────────────

def git(cwd, *args, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def live_runs() -> list[str]:
    out = subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True).stdout
    return [l.strip()[:120] for l in out.splitlines()
            if any(k in l for k in ("sweep.sh", "supervisor.sh", "/work.py", " work.py"))
            and "bench.py" not in l and "grep" not in l]


# ─── make ─────────────────────────────────────────────────────────────────────

def cmd_make(a) -> int:
    project = Path(a.project).expanduser().resolve()
    main = a.main_branch
    velocity = {}
    vp = project / "logs" / "velocity.jsonl"
    if vp.exists():
        for line in vp.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("outcome") == "done":
                velocity[r["task"]] = r            # task text, truncated to 120

    cases, skipped = [], []
    merges = git(project, "log", "--first-parent", "--merges", "--format=%H", main).split()
    for merge in reversed(merges):
        parents = git(project, "rev-list", "--parents", "-n1", merge).split()
        if len(parents) != 3:
            continue
        _, base, branch_tip = parents
        msg = git(project, "log", "-1", "--format=%s", branch_tip).strip()
        m = re.match(r"task \d+: (.+)", msg)
        if not m:
            skipped.append((merge[:8], "not a task merge"))
            continue
        roadmap = git(project, "show", f"{base}:ROADMAP.md")
        line = find_task_line(roadmap, m.group(1))
        if not line:
            skipped.append((merge[:8], "task line not found / ambiguous"))
            continue
        gate = extract_gate(line)
        if not gate:
            skipped.append((merge[:8], "no task gate"))
            continue
        text = task_text(line)
        hist = next((v for k, v in velocity.items() if text.startswith(k[:100])), None)
        cases.append({
            "id": f"{slug(target_of(text) or text, 30)}-{merge[:7]}",
            "merge": merge, "base": base, "task_line": line,
            "target": target_of(text), "gate": gate,
            "history": ({"model": hist.get("model"), "attempts": hist.get("attempts"),
                         "duration_s": hist.get("duration_s")} if hist else None),
        })

    if a.verify and cases:
        print(f"verifying {len(cases)} gates at their merge commits…", flush=True)
        vdir = WORK / "_verify"
        shutil.rmtree(vdir, ignore_errors=True)
        vdir.parent.mkdir(parents=True, exist_ok=True)
        git(vdir.parent, "clone", "-q", str(project), str(vdir))
        for link in a.link:
            if (project / link).exists():
                (vdir / link).symlink_to(project / link)
        kept = []
        for c in cases:
            git(vdir, "checkout", "-q", "-f", c["merge"])
            r = subprocess.run(["bash", "-lc", no_pub(c["gate"])], cwd=vdir, capture_output=True,
                               text=True, timeout=600)
            (kept if r.returncode == 0 else skipped).append(
                c if r.returncode == 0 else (c["merge"][:8], "gate fails at merge"))
        cases = kept
        shutil.rmtree(vdir, ignore_errors=True)

    suite = {
        "name": a.name, "project": str(project), "main_branch": main,
        "link": a.link, "created": datetime.now().isoformat(timespec="seconds"),
        "cases": cases,
    }
    SUITES.mkdir(parents=True, exist_ok=True)
    out = SUITES / f"{a.name}.json"
    out.write_text(json.dumps(suite, indent=2) + "\n")
    print(f"{len(cases)} case(s) → {out.relative_to(HERE)}")
    for mid, why in skipped:
        print(f"  skipped {mid}: {why}")
    return 0


# ─── run ──────────────────────────────────────────────────────────────────────

def prepare(case: dict, suite: dict, config: dict | None = None) -> Path:
    wd = WORK / case["id"]
    shutil.rmtree(wd, ignore_errors=True)
    wd.parent.mkdir(parents=True, exist_ok=True)
    src = Path(suite["project"])
    git(wd.parent, "clone", "-q", "--no-checkout", str(src), str(wd))
    main = suite.get("main_branch", "main")
    git(wd, "checkout", "-q", "-B", main, case["base"])
    git(wd, "config", "user.email", "bench@xanadu.local")
    git(wd, "config", "user.name", "xanadu-bench")
    git(wd, "remote", "remove", "origin", check=False)   # never push anywhere
    exclude = wd / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a") as f:
        for link in suite.get("link", []):
            if (src / link).exists():
                (wd / link).symlink_to(src / link)
                f.write(f"\n/{link}\n")
    rm = wd / "ROADMAP.md"
    rm.write_text(only_open(rm.read_text(), case["task_line"]))
    if config:                                  # e.g. {"rag_enabled": true}
        cp = wd / ".sovereign_config.json"
        cfg = json.loads(cp.read_text()) if cp.exists() else {}
        cfg.update(config)
        cp.write_text(json.dumps(cfg, indent=2) + "\n")
    git(wd, "add", "ROADMAP.md", *([".sovereign_config.json"] if config else []))
    git(wd, "commit", "-qm", f"bench: only {case['id']} open")
    (wd / "logs").mkdir(exist_ok=True)
    return wd


def run_case(case: dict, suite: dict, env: dict, timeout: int, outdir: Path,
             config: dict | None = None) -> dict:
    wd = prepare(case, suite, config)
    log = outdir / f"{case['id']}.log"
    t0 = time.time()
    timed_out = False
    with open(log, "w") as f:
        try:
            r = subprocess.run([PYTHON, "-u", str(HERE / "work.py"), "--project", str(wd),
                                "--no-retry"], cwd=HERE, env=env, stdout=f,
                               stderr=subprocess.STDOUT, timeout=timeout)
            exit_code = r.returncode
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, None
    wall = round(time.time() - t0, 1)

    marked = "- [x] " + task_text(case["task_line"]) in (wd / "ROADMAP.md").read_text()
    main = suite.get("main_branch", "main")
    git(wd, "checkout", "-q", "-f", main, check=False)
    g = subprocess.run(["bash", "-lc", no_pub(case["gate"])], cwd=wd, capture_output=True,
                       text=True, timeout=600)
    vel = {}
    vp = wd / "logs" / "velocity.jsonl"
    if vp.exists():
        lines = [l for l in vp.read_text().splitlines() if l.strip()]
        if lines:
            vel = json.loads(lines[-1])
    shutil.copytree(wd / "logs", outdir / f"{case['id']}-logs", dirs_exist_ok=True)
    shutil.rmtree(wd, ignore_errors=True)
    return {
        "case": case["id"], "target": case["target"],
        "passed": bool(marked and g.returncode == 0), "marked": marked,
        "gate_ok": g.returncode == 0, "attempts": vel.get("attempts"),
        "outcome": vel.get("outcome"), "model": vel.get("model"),
        "harness_s": vel.get("duration_s"), "wall_s": wall,
        "exit_code": exit_code, "timed_out": timed_out,
    }


# ─── Aider engine arm ─────────────────────────────────────────────────────────
# Same case, same model (the profile's tier 1), same gate. Aider edits with
# its repo map and its own test loop (--test-cmd + --auto-test). It has no
# ROADMAP to tick and no lock rules, so a pass = the gate passes AND no test,
# spec or locked file changed (rewriting the test to pass is not a pass).

def profile_model(profile: str) -> tuple[str, str]:
    import tomllib
    t = tomllib.loads((HERE / "profiles" / f"{profile}.toml").read_text())
    tier = t.get("tiers", {}).get("1", {})
    return tier.get("model", ""), tier.get("url", "")


def gate_files(gate: str) -> list[str]:
    """Test files a gate names: `pytest -q "tests/unit/test_x.py::test_a"` → tests/unit/test_x.py."""
    return sorted({m.split("::")[0] for m in re.findall(r"[\w./-]+\.(?:py|dart|ts|go)(?:::[\w\[\]-]+)?", gate)})


def protected_paths(wd: Path) -> tuple[list[str], list[str]]:
    try:
        cfg = json.loads((wd / ".sovereign_config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    prefixes = list(cfg.get("locked_prefixes", [])) + ["tests/", "test/", "spec/", "ROADMAP.md"]
    return prefixes, list(cfg.get("locked_files", []))


def aider_cmd(case: dict, model: str, files: list[str], timeout: int) -> list[str]:
    return ["aider", "--model", f"ollama_chat/{model}",
            "--message", task_text(case["task_line"]),
            "--test-cmd", case["gate"], "--auto-test",
            "--yes-always", "--no-auto-commits", "--no-check-update", "--no-analytics",
            "--no-show-model-warnings", "--no-pretty", "--no-stream", "--no-fancy-input",
            "--no-gitignore", "--timeout", str(timeout), *files]


def run_case_aider(case: dict, suite: dict, env: dict, timeout: int, outdir: Path,
                   config: dict | None = None, profile: str = "") -> dict:
    wd = prepare(case, suite, config)
    model, url = profile_model(profile)
    env = {**env, "OLLAMA_API_BASE": url or env.get("OLLAMA_URL", "http://localhost:11434")}
    tests = [f for f in gate_files(case["gate"]) if (wd / f).exists()]
    files = [case["target"]] + tests            # target editable; aider adds more from its map
    log = outdir / f"{case['id']}.log"
    t0 = time.time()
    timed_out, exit_code = False, None
    with open(log, "w") as f:
        f.write("$ " + " ".join(aider_cmd(case, model, files, timeout)) + "\n\n")
        f.flush()
        try:
            r = subprocess.run(aider_cmd(case, model, files, timeout), cwd=wd, env=env,
                               stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               timeout=timeout)
            exit_code = r.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    wall = round(time.time() - t0, 1)

    changed = [l[3:].strip() for l in git(wd, "status", "--porcelain", check=False).splitlines()]
    prefixes, locked = protected_paths(wd)
    cheated = [c for c in changed if c in locked or any(c.startswith(p) for p in prefixes)]
    g = subprocess.run(["bash", "-lc", no_pub(case["gate"])], cwd=wd, capture_output=True,
                       text=True, timeout=600)
    shutil.rmtree(wd, ignore_errors=True)
    return {
        "case": case["id"], "target": case["target"], "engine": "aider",
        "passed": g.returncode == 0 and not cheated and not timed_out, "marked": None,
        "gate_ok": g.returncode == 0, "edited_protected": cheated,
        "files_changed": changed, "attempts": None, "outcome": None, "model": model,
        "harness_s": None, "wall_s": wall, "exit_code": exit_code, "timed_out": timed_out,
    }


def cmd_run(a) -> int:
    suite = json.loads((SUITES / f"{a.suite}.json").read_text())
    live = live_runs()
    if live and not a.force:
        print("refusing: a run is using Ollama, so timings would be meaningless:")
        for l in live[:4]:
            print(f"  {l}")
        print("stop it first, or pass --force to benchmark anyway (results flagged).")
        return 1
    cases = suite["cases"]
    if a.cases:
        want = set(a.cases.split(","))
        cases = [c for c in cases if c["id"] in want]
    if a.tag:
        cases = [c for c in cases if tag_of(c) == a.tag]
    if a.sample:
        cases = spread(cases, a.sample)
    if a.limit:
        cases = cases[:a.limit]

    env = {**os.environ, "SOVEREIGN_PROFILE": a.profile,
           "MAIN_BRANCH": suite.get("main_branch", "main"),
           "FIRESTORE_PROJECT_ID": ""}
    for kv in a.set:
        k, _, v = kv.partition("=")
        env[k] = v
    config = {}
    for kv in a.config:
        k, _, v = kv.partition("=")
        config[k] = json.loads(v)
    label = a.label or a.profile
    outdir = RESULTS / f"{datetime.now():%Y%m%d-%H%M}-{slug(label, 30)}"
    outdir.mkdir(parents=True, exist_ok=True)
    meta = {"suite": a.suite, "profile": a.profile, "engine": a.engine,
            "set": a.set, "config": config, "label": label,
            "started": datetime.now().isoformat(timespec="seconds"),
            "forced_during_live_run": bool(live), "cases": [c["id"] for c in cases]}
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"{len(cases)} case(s) · profile {a.profile} {' '.join(a.set)} → "
          f"{outdir.relative_to(HERE)}", flush=True)
    records = []
    with open(outdir / "results.jsonl", "w") as rf:
        for i, c in enumerate(cases, 1):
            if a.stop_at and past(a.stop_at, meta["started"]):
                print(f"stop-at {a.stop_at} reached — {len(cases) - i + 1} case(s) not started")
                break
            print(f"[{i}/{len(cases)}] {c['id']} …", end=" ", flush=True)
            if a.engine == "aider":
                rec = run_case_aider(c, suite, env, a.timeout, outdir, config, a.profile)
            else:
                rec = run_case(c, suite, env, a.timeout, outdir, config)
            rec["tag"] = tag_of(c)
            records.append(rec)
            rf.write(json.dumps(rec) + "\n")
            rf.flush()
            verdict = "PASS" if rec["passed"] else ("TIMEOUT" if rec["timed_out"] else "fail")
            extra = " (harness said done, gate FAILED)" if rec["marked"] and not rec["gate_ok"] else ""
            if rec.get("edited_protected"):
                extra = f" (gate {'passed' if rec['gate_ok'] else 'failed'} but edited {', '.join(rec['edited_protected'][:2])})"
            tries = f"{rec['attempts']} attempt(s) · " if rec.get("attempts") is not None else ""
            print(f"{verdict} · {tries}{rec['wall_s']:.0f}s{extra}", flush=True)
    s = summarize(records)
    (outdir / "summary.json").write_text(json.dumps(s, indent=2) + "\n")
    med = f"median {s['median_wall_s_passed']:.0f}s per pass" if s["median_wall_s_passed"] else "no passes"
    print(f"\n{s['passed']}/{s['cases']} passed ({s['pass_rate']:.0%}), "
          f"{s['first_attempt']} on the first attempt, {med}, {s['total_wall_s'] / 60:.0f} min total")
    if s["harness_lied"]:
        print(f"⚠ {s['harness_lied']} case(s) marked done whose gate fails")
    return 0


def past(stop_at: str, started_iso: str) -> bool:
    """True once the clock passes HH:MM, counting from the run's start (an
    08:00 stop set at 18:00 means tomorrow morning)."""
    started = datetime.fromisoformat(started_iso)
    hh, mm = map(int, stop_at.split(":"))
    stop = started.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop <= started:
        stop += timedelta(days=1)
    return datetime.now() >= stop


def tag_of(case: dict) -> str:
    """easy = passed first try on a <20B model; hard = needed a big model or
    retries; unknown = no velocity record."""
    h = case.get("history") or {}
    if not h:
        return "unknown"
    model = h.get("model") or ""
    big = any(s in model for s in ("27b", "30b", "32b", "35b", "70b"))
    return "easy" if (h.get("attempts") == 1 and not big) else "hard"


# ─── compare ──────────────────────────────────────────────────────────────────

def load_run(path: str) -> tuple[dict, dict]:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = RESULTS / path
    meta = json.loads((p / "meta.json").read_text())
    recs = {json.loads(l)["case"]: json.loads(l)
            for l in (p / "results.jsonl").read_text().splitlines() if l.strip()}
    return meta, recs


def cmd_compare(a) -> int:
    ma, ra = load_run(a.a)
    mb, rb = load_run(a.b)
    la, lb = ma["label"], mb["label"]
    ids = [c for c in ra if c in rb]

    def cell(r):
        if r["passed"]:
            return f"PASS {str(r['attempts']) + '× ' if r.get('attempts') is not None else ''}{r['wall_s']:.0f}s"
        return "TIMEOUT" if r["timed_out"] else f"fail {r['wall_s']:.0f}s"

    w = max([len(i) for i in ids] + [4])
    print(f"{'case':<{w}}  {la:<20}  {lb:<20}")
    for i in ids:
        flag = "  ◀" if ra[i]["passed"] != rb[i]["passed"] else ""
        print(f"{i:<{w}}  {cell(ra[i]):<20}  {cell(rb[i]):<20}{flag}")
    sa, sb = summarize([ra[i] for i in ids]), summarize([rb[i] for i in ids])
    print()
    for k, name in (("passed", "passed"), ("first_attempt", "first attempt"),
                    ("median_wall_s_passed", "median s / pass"), ("total_wall_s", "total s")):
        print(f"{name:<16} {str(sa[k]):>10}  {str(sb[k]):>10}")
    if ma.get("forced_during_live_run") or mb.get("forced_during_live_run"):
        print("\n⚠ at least one run was forced during a live run — timings are not comparable")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="build a suite from a project's merged tasks")
    m.add_argument("--project", required=True)
    m.add_argument("--name", required=True)
    m.add_argument("--main-branch", default="main")
    m.add_argument("--link", nargs="*", default=[".venv", "node_modules", ".dart_tool"],
                   help="untracked dirs to symlink into each case clone")
    m.add_argument("--no-verify", dest="verify", action="store_false",
                   help="skip re-running each gate at its merge commit")
    r = sub.add_parser("run", help="run a suite with a profile")
    r.add_argument("--suite", required=True)
    r.add_argument("--profile", required=True)
    r.add_argument("--set", action="append", default=[], metavar="KEY=VAL",
                   help="env override for work.py, e.g. OLLAMA_THINK=0 (repeatable)")
    r.add_argument("--config", action="append", default=[], metavar="KEY=JSON",
                   help="override a .sovereign_config.json key in each case clone, "
                        "e.g. rag_enabled=true (repeatable)")
    r.add_argument("--label")
    r.add_argument("--cases", help="comma-separated case ids")
    r.add_argument("--tag", choices=["easy", "hard", "unknown"])
    r.add_argument("--limit", type=int)
    r.add_argument("--sample", type=int, help="N cases spread across different target files")
    r.add_argument("--timeout", type=int, default=3900, help="seconds per case")
    r.add_argument("--force", action="store_true", help="run even while a sweep is live")
    r.add_argument("--engine", choices=["xanadu", "aider"], default="xanadu",
                   help="aider: same model and gate, Aider does the editing")
    r.add_argument("--stop-at", metavar="HH:MM", help="start no new case after this time")
    c = sub.add_parser("compare", help="compare two runs case by case")
    c.add_argument("a")
    c.add_argument("b")
    a = ap.parse_args()
    return {"make": cmd_make, "run": cmd_run, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
