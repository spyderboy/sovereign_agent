"""Run control shared by the console, xrun.py and bench/overnight.py.

A run plan (runs/<project>.json) holds every per-run decision: which model
each pass uses and where it runs (this machine, a RunPod pod, another server),
workers, time caps, thinking, per-attempt limits, the RunPod budget. From it
this module writes two profiles (profiles/run-<project>-{cheap,strong}.toml),
starts sweep.sh + monitor.py + a pod guard, and stops them — always with the
safe sequence from the xanadu-run-control skill: stop the monitor first (or
it restarts the sweep), kill, park the repo clean, verify, then launch.

RunPod pods are started only for passes that use them (sweep.sh calls
`xrun.py hook` before each pass) and a guard stops them when the run ends or
the plan's budget is reached.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RUNS = HERE / "runs"
PROFILES = HERE / "profiles"
PY = str(HERE / ".venv" / "bin" / "python") if (HERE / ".venv").exists() else sys.executable

PASS_DEFAULTS = {
    "cheap": {"model": "qwen2.5-coder:14b", "params": 14, "ctx": 16384, "where": "local",
              "url": "", "workers": 2, "cap_minutes": 90, "think": "default",
              "call_timeout_s": 0, "planner_model": "",
              "escalation_model": "", "escalation_url": ""},
    "strong": {"model": "qwen3.8:27b", "params": 27, "ctx": 16384, "where": "local",
               "url": "", "workers": 1, "cap_minutes": 120, "think": "default",
               "call_timeout_s": 900, "planner_model": "",
              "escalation_model": "", "escalation_url": ""},
}
PLAN_DEFAULTS = {
    "main_branch": "main",
    "max_cycles": 5,
    "task_ceiling_s": 3600,
    "attempts": {"tier1": 3, "mid": 2, "last": 2},
    "runpod": {"gpu_type": "NVIDIA L40S", "gpu_count": 1, "volume_gb": 200,
               "cloud": "SECURE", "max_run_usd": 40.0, "after_run": "stop",
               "stop_between_passes": True},
    "monitor": {"restart": True, "ntfy_topic": ""},
}


# ─── plans ────────────────────────────────────────────────────────────────────

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def plan_path(project: str) -> Path:
    return RUNS / f"{slug(os.path.basename(os.path.realpath(project)))}.json"


def load_plan(project: str) -> dict:
    p = plan_path(project)
    saved = json.loads(p.read_text()) if p.exists() else {}
    plan = json.loads(json.dumps(PLAN_DEFAULTS))
    for k, v in saved.items():
        if isinstance(v, dict) and isinstance(plan.get(k), dict):
            plan[k].update(v)
        else:
            plan[k] = v
    for name, dflt in PASS_DEFAULTS.items():
        plan[name] = {**dflt, **saved.get(name, {})}
    plan["project"] = os.path.realpath(os.path.expanduser(project))
    return plan


def validate(plan: dict) -> list[str]:
    errs = []
    for name in ("cheap", "strong"):
        p = plan[name]
        if not p.get("model"):
            errs.append(f"{name}: choose a model")
        if p.get("where") not in ("local", "runpod", "url"):
            errs.append(f"{name}: where must be local, runpod or url")
        if p.get("where") == "url" and not str(p.get("url", "")).startswith(("http://", "https://")):
            errs.append(f"{name}: enter the server URL (http://… or https://…)")
        eu = str(p.get("escalation_url") or "")
        if eu and not eu.startswith(("http://", "https://")):
            errs.append(f"{name}: the final-tier server must be a URL (http://… or https://…)")
        if eu and not p.get("escalation_model"):
            errs.append(f"{name}: a final-tier server needs a final-tier model")
        if not 1 <= int(p.get("workers", 1)) <= 4:
            errs.append(f"{name}: workers must be 1–4")
        if int(p.get("cap_minutes", 0)) < 10:
            errs.append(f"{name}: time cap must be at least 10 minutes")
        if p.get("think") not in ("default", "on", "off", "first-only"):
            errs.append(f"{name}: thinking must be default, on, off or first-only")
    if plan["strong"].get("where") == "runpod" or plan["cheap"].get("where") == "runpod":
        if float(plan["runpod"].get("max_run_usd", 0)) <= 0:
            errs.append("RunPod: set a spending cap for the run")
    return errs


def save_plan(plan: dict) -> list[str]:
    errs = validate(plan)
    if errs:
        return errs
    RUNS.mkdir(exist_ok=True)
    keep = {k: v for k, v in plan.items() if k != "project"}
    keep["project"] = plan["project"]
    plan_path(plan["project"]).write_text(json.dumps(keep, indent=2) + "\n")
    return []


# ─── profiles ─────────────────────────────────────────────────────────────────

def profile_name(plan: dict, pass_name: str) -> str:
    return f"run-{slug(os.path.basename(plan['project']))}-{pass_name}"


def profile_text(plan: dict, pass_name: str, url: str = "") -> str:
    p = plan[pass_name]
    lines = [f"# Generated by the Xanadu console from {plan_path(plan['project']).name}.",
             "# Edit the run plan in the console, not this file.", ""]
    for i in range(1, 5):
        lines += [f"[tiers.{i}]", f'model  = "{p["model"]}"', f"params = {float(p['params'])}",
                  f"ctx    = {int(p['ctx'])}"]
        if url:
            lines.append(f'url    = "{url}"')
        lines.append("")
    a = plan["attempts"]
    lines += ["[budget]", f"task_ceiling_s = {int(plan['task_ceiling_s'])}",
              f"attempts_tier1 = {int(a['tier1'])}", f"attempts_mid   = {int(a['mid'])}",
              f"attempts_last  = {int(a['last'])}", f"quick_param_limit_b = {float(p['params']) + 1}"]
    if int(p.get("call_timeout_s") or 0):
        lines.append(f"call_timeout_s = {int(p['call_timeout_s'])}")
    lines += ["", "[run]", f'main_branch = "{plan["main_branch"]}"']
    if p.get("planner_model"):
        lines.append(f'planner_model = "{p["planner_model"]}"')
    if p.get("escalation_model"):
        lines.append(f'escalation_model = "{p["escalation_model"]}"')
        if p.get("escalation_url"):
            lines.append(f'escalation_url = "{p["escalation_url"].rstrip("/")}"')
    think = p.get("think", "default")
    if think == "on":
        lines.append("think = true")
    elif think == "off":
        lines.append("think = false")
    elif think == "first-only":
        lines.append("think_retries = false")
    return "\n".join(lines) + "\n"


def pass_url(plan: dict, pass_name: str, state: dict) -> str:
    p = plan[pass_name]
    if p["where"] == "url":
        return p["url"].rstrip("/")
    if p["where"] == "runpod":
        pod = state.get("pods", {}).get(pass_name)
        return f"https://{pod}-11434.proxy.runpod.net" if pod else ""
    return ""


def write_profiles(plan: dict, state: dict | None = None) -> dict:
    state = state or load_state(plan)
    out = {}
    for name in ("cheap", "strong"):
        pn = profile_name(plan, name)
        (PROFILES / f"{pn}.toml").write_text(profile_text(plan, name, pass_url(plan, name, state)))
        out[name] = pn
    return out


# ─── state (pods, run start) ──────────────────────────────────────────────────

def state_path(plan: dict) -> Path:
    return RUNS / f"{slug(os.path.basename(plan['project']))}.state.json"


def load_state(plan: dict) -> dict:
    p = state_path(plan)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(plan: dict, state: dict) -> None:
    RUNS.mkdir(exist_ok=True)
    state_path(plan).write_text(json.dumps(state, indent=2) + "\n")


# ─── processes and repo ───────────────────────────────────────────────────────

def _ps() -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), args))
    return rows


def processes(project: str) -> dict[str, list[int]]:
    """Run-related processes for this project, matched by the script each one
    actually runs (sov/procs.py) — never shells, editors or viewers that merely
    mention a script name and the path."""
    from sov.procs import mentions, script_of
    root = os.path.realpath(project)
    found = {"sweep": [], "supervisor": [], "work": [], "monitor": [], "guard": [], "overnight": []}
    names = {"sweep.sh": "sweep", "supervisor.sh": "supervisor", "work.py": "work",
             "monitor.py": "monitor", "overnight.py": "overnight"}
    for pid, args in _ps():
        script, rest = script_of(args)
        if not script or not mentions(rest, root):
            continue
        if script == "xrun.py" and rest[:1] == ["guard"]:
            found["guard"].append(pid)
        elif script in names:
            found[names[script]].append(pid)
    return found


def is_running(project: str) -> bool:
    p = processes(project)
    return bool(p["sweep"] or p["supervisor"] or p["work"])


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def park_repo(root: str, main: str) -> list[str]:
    """Return the repo to a clean main after workers are gone. Returns the
    uncommitted changes it refused to touch (non-empty = don't launch).

    The rule that keeps human work safe: uncommitted changes while HEAD is a
    task-* branch are worker debris (single-worker runs work in the main
    checkout) and are discarded; uncommitted changes on main are a human edit
    and are NEVER discarded. The check happens before any forced checkout.
    """
    if os.path.exists(os.path.join(root, ".git", "MERGE_HEAD")):
        _git(root, "merge", "--abort")         # an interrupted worker merge
    for dirpath, _, files in os.walk(os.path.join(root, ".git")):
        for f in files:
            if f.endswith(".lock"):
                os.remove(os.path.join(dirpath, f))
    head = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = [l for l in _git(root, "status", "--porcelain").stdout.splitlines()
             if l and not l[3:].startswith("logs/")]
    if dirty and not head.startswith("task-"):
        return dirty                           # a human edit: refuse, touch nothing
    for line in _git(root, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree ") and "xanadu-worktrees" in line:
            _git(root, "worktree", "remove", "--force", line.split(" ", 1)[1])
    _git(root, "worktree", "prune")
    _git(root, "checkout", "-q", "-f", main)
    if head.startswith("task-"):
        _git(root, "clean", "-fdq", "-e", "logs")   # the dead task's untracked files
    for b in _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/task-*").stdout.split():
        _git(root, "branch", "-D", b)
    return [l for l in _git(root, "status", "--porcelain").stdout.splitlines()
            if l and not l[3:].startswith("logs/")]


def stop(plan: dict, stop_pods: bool = True) -> dict:
    root = plan["project"]
    procs = processes(root)
    for pid in procs["monitor"] + procs["guard"] + procs["overnight"]:
        _kill(pid, signal.SIGTERM)             # watchers first, or they relaunch the run
    time.sleep(1)
    for pid in procs["sweep"] + procs["supervisor"] + procs["work"]:
        _kill(pid, signal.SIGKILL)
    time.sleep(2)
    left = is_running(root)
    dirty = [] if left else park_repo(root, plan["main_branch"])
    pods = stop_run_pods(plan) if stop_pods else []
    return {"stopped": not left, "dirty": dirty, "pods_stopped": pods}


def _spawn(args, **kw):
    """Start a detached process (a seam for tests)."""
    return subprocess.Popen(args, **kw)


def _kill(pid: int, sig) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


# ─── RunPod lifecycle ─────────────────────────────────────────────────────────

def ensure_pod(plan: dict, pass_name: str, log=print) -> str:
    """Start (or create) this pass's pod, wait for Ollama, pull the model.
    Returns the endpoint URL."""
    from sov import runpod
    state = load_state(plan)
    rp = plan["runpod"]
    spent = runpod.spend_since(state.get("started_at") or _now())
    if spent >= float(rp["max_run_usd"]):
        raise runpod.RunPodError(f"run budget reached (${spent:.2f} of ${rp['max_run_usd']})")
    name = f"{runpod.PREFIX}{slug(os.path.basename(plan['project']))}-{pass_name}"
    pods = {p["name"]: p for p in runpod.list_pods()}
    pod = pods.get(name)
    if pod and pod.get("desiredStatus") == "RUNNING":
        log(f"pod {name} already running")
    elif pod:
        log(f"starting pod {name}")
        runpod.start_pod(pod["id"])
    else:
        log(f"creating pod {name} ({rp['gpu_count']}× {rp['gpu_type']}, {rp['cloud'].lower()} cloud)")
        pod = runpod.create_pod(name, rp["gpu_type"], int(rp["gpu_count"]),
                                int(rp["volume_gb"]), rp["cloud"])
    url = runpod.endpoint(pod["id"])
    state.setdefault("pods", {})[pass_name] = pod["id"]
    save_state(plan, state)
    log(f"waiting for Ollama at {url}")
    if not runpod.wait_ready(url):
        raise runpod.RunPodError(f"pod {name} did not become ready in 15 minutes")
    model = plan[pass_name]["model"]
    if model not in runpod.models_at(url):
        log(f"pulling {model} into the pod (first time only — it stays on the volume)")
        runpod.pull_model(url, model)
    log(f"pod ready: {url}")
    return url


def stop_run_pods(plan: dict, only: str | None = None) -> list[str]:
    """Stop (or terminate, per plan) this project's pods. Never raises."""
    try:
        from sov import runpod
        state = load_state(plan)
        done = []
        for pass_name, pod_id in state.get("pods", {}).items():
            if only and pass_name != only:
                continue
            try:
                p = runpod.get_pod(pod_id)
            except runpod.RunPodError:
                continue
            if p.get("desiredStatus") != "RUNNING":
                continue
            if plan["runpod"].get("after_run") == "terminate" and not only:
                runpod.terminate_pod(pod_id)
            else:
                runpod.stop_pod(pod_id)
            done.append(pod_id)
        return done
    except Exception:
        return []


def uses_runpod(plan: dict) -> bool:
    return "runpod" in (plan["cheap"]["where"], plan["strong"]["where"])


# ─── launch / hook / guard ────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sweep_env(plan: dict, profiles: dict) -> dict:
    c, s = plan["cheap"], plan["strong"]
    return {"MAIN_BRANCH": plan["main_branch"], "CHEAP": profiles["cheap"],
            "STRONG": profiles["strong"], "CHEAP_WORKERS": str(c["workers"]),
            "STRONG_WORKERS": str(s["workers"]), "CHEAP_MAX": str(int(c["cap_minutes"]) * 60),
            "STRONG_MAX": str(int(s["cap_minutes"]) * 60), "MAX_CYCLES": str(plan["max_cycles"])}


def launch(plan: dict, log=print) -> dict:
    root = plan["project"]
    errs = validate(plan)
    if errs:
        return {"ok": False, "error": "; ".join(errs)}
    if is_running(root):
        return {"ok": False, "error": "a run is already live on this project — stop it first"}
    for pid in processes(root)["monitor"] + processes(root)["guard"]:
        _kill(pid, signal.SIGTERM)
    dirty = park_repo(root, plan["main_branch"])
    if dirty:
        return {"ok": False, "error": "the project has uncommitted changes — commit or discard "
                "them first (never treated as debris): " + ", ".join(dirty[:5])}
    state = {"started_at": _now(), "pods": load_state(plan).get("pods", {})}
    save_state(plan, state)
    profiles = write_profiles(plan, state)
    env = sweep_env(plan, profiles)
    logs = Path(root, "logs")
    logs.mkdir(exist_ok=True)
    hook = f"{PY} {HERE / 'xrun.py'} hook {root}"
    senv = {**os.environ, **env, "PASS_HOOK": hook}
    _spawn(["./sweep.sh", root], cwd=HERE, env=senv,
                     stdout=open(logs / "sweep-console.log", "a"), stderr=subprocess.STDOUT,
                     start_new_session=True)
    env_str = " ".join(f"{k}={v}" for k, v in {**env, "PASS_HOOK": hook}.items() if k != "PASS_HOOK")
    margs = [PY, "-u", str(HERE / "monitor.py"), root, "--summary-every", "60"]
    if plan["monitor"].get("restart"):
        margs += ["--restart", env_str + f" PASS_HOOK='{hook}'"]
    if plan["monitor"].get("ntfy_topic"):
        margs += ["--ntfy-topic", plan["monitor"]["ntfy_topic"]]
    _spawn(margs, cwd=HERE, stdout=open(logs / "monitor.log", "a"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    if uses_runpod(plan):
        _spawn([PY, "-u", str(HERE / "xrun.py"), "guard", root], cwd=HERE,
                         stdout=open(logs / "podguard.log", "a"), stderr=subprocess.STDOUT,
                         start_new_session=True)
    log(f"launched: {env_str}")
    return {"ok": True, "env": env}


def hook(plan: dict, label: str, log=print) -> int:
    """Called by sweep.sh before each pass: `cycle2-strong` etc. Starts the
    pass's pod (and rewrites its profile with the pod URL); stops the other
    pass's pod if the plan says so. Non-zero exit = sweep.sh skips the pass."""
    pass_name = "strong" if label.endswith("-strong") else "cheap"
    other = "cheap" if pass_name == "strong" else "strong"
    if plan["runpod"].get("stop_between_passes") and plan[other]["where"] == "runpod":
        stopped = stop_run_pods(plan, only=other)
        if stopped:
            log(f"stopped idle {other} pod(s) {stopped} before {label}")
    if plan[pass_name]["where"] != "runpod":
        return 0
    try:
        ensure_pod(plan, pass_name, log)
    except Exception as exc:
        log(f"pod for {label} unavailable: {exc}")
        return 1
    write_profiles(plan)
    return 0


def guard(plan: dict, log=print, every_s: int = 300) -> None:
    """Stop the run's pods when the run ends or the budget is reached."""
    from sov import runpod
    root = plan["project"]
    quiet = 0
    while True:
        time.sleep(every_s)
        state = load_state(plan)
        spent = runpod.spend_since(state.get("started_at") or _now())
        cap = float(plan["runpod"]["max_run_usd"])
        if spent >= cap:
            stopped = stop_run_pods(plan)
            log(f"budget reached: ${spent:.2f} of ${cap:.2f} — stopped pods {stopped}")
            _notify(plan, f"RunPod budget reached (${spent:.2f}); pods stopped")
            return
        quiet = quiet + 1 if not is_running(root) else 0
        if quiet >= 2:
            stopped = stop_run_pods(plan)
            log(f"run over — pods {plan['runpod'].get('after_run', 'stop')}ped: {stopped}; "
                f"spent ${spent:.2f}")
            return


def _notify(plan: dict, msg: str) -> None:
    try:
        sys.path.insert(0, str(HERE))
        import monitor
        monitor.Notifier(os.path.basename(plan["project"]), True,
                         plan["monitor"].get("ntfy_topic") or None).send(msg)
    except Exception:
        pass


# ─── status ───────────────────────────────────────────────────────────────────

def status(plan: dict) -> dict:
    root = plan["project"]
    road = Path(root, "ROADMAP.md")
    txt = road.read_text(errors="ignore") if road.exists() else ""
    done = len(re.findall(r"^- \[x\]", txt, re.M))
    total = len(re.findall(r"^- \[[ x]\]", txt, re.M))
    procs = processes(root)
    sweep_log = Path(root, "logs", "sweep.log")
    last_pass = ""
    if sweep_log.exists():
        for line in sweep_log.read_text(errors="ignore").splitlines():
            m = re.search(r"(cycle\d+-\w+)\s+profile=", line)
            if m:
                last_pass = m.group(1)
    mon = Path(root, "logs", "monitor.log")
    alerts, recent = [], []
    if mon.exists():
        lines = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in mon.read_text(errors="ignore").splitlines()]
        alerts = [l for l in lines if "⚠" in l][-5:]
        recent = [l for l in lines if l.strip()][-12:]
    state = load_state(plan)
    spent = None
    if uses_runpod(plan) and state.get("started_at"):
        try:
            from sov import runpod
            spent = runpod.spend_since(state["started_at"])
        except Exception:
            spent = None
    return {"project": root, "done": done, "total": total, "running": is_running(root),
            "processes": {k: len(v) for k, v in procs.items()}, "pass": last_pass,
            "alerts": alerts, "recent": recent, "pods": state.get("pods", {}),
            "started_at": state.get("started_at"), "spent_usd": spent,
            "budget_usd": plan["runpod"]["max_run_usd"] if uses_runpod(plan) else None}
