#!/usr/bin/env python3
"""mcp_server.py — Xanadu run control over MCP (stdio).

    claude mcp add xanadu -- ~/Code/Xanadu/sovereign_agent/.venv/bin/python \\
        ~/Code/Xanadu/sovereign_agent/mcp_server.py

Every tool calls sov/runctl.py — the same code as the console and xrun.py —
so a launch from an MCP client gets the safe sequence (stop watchers, kill,
park the repo clean, refuse uncommitted changes or broken finished gates,
then sweep + monitor + pod guard). Nothing here reimplements run control.

Why (2026-10-03): a night of manual stop/edit/commit/relaunch cycles on the
xmeasure v2 run, each one a chance to strand work. Fewer hand-typed command
sequences means fewer operator mistakes as the number of projects grows.

Tools that change state (launch_run, stop_run) are annotated destructive so a
client asks before calling them. Plan editing stays in the console for now.

stdout is the MCP protocol channel: every tool runs with stdout redirected to
stderr, because runctl and preflight print progress.
"""
import contextlib
import functools
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from sov import runctl  # noqa: E402

server = MCPServer(
    name="xanadu",
    instructions=(
        "Run control for Xanadu, an autonomous local-LLM coding harness. A project is a "
        "repository with a ROADMAP.md of tasks; a run sweeps it with a cheap model pass and a "
        "strong model pass until nothing more moves. Check run_status before launching or "
        "stopping. launch_run refuses on uncommitted changes or broken finished gates — report "
        "the refusal, never work around it."),
)

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def _quiet(fn):
    """Keep stdout clean for the protocol."""
    @functools.wraps(fn)
    def inner(*a, **k):
        with contextlib.redirect_stdout(sys.stderr):
            return fn(*a, **k)
    return inner


def resolve(project: str) -> str:
    """A project name (as list_projects shows it) or a path → its absolute path."""
    p = os.path.realpath(os.path.expanduser(project))
    if os.path.isfile(os.path.join(p, "ROADMAP.md")):
        return p
    for plan_file in sorted(runctl.RUNS.glob("*.json")):
        if plan_file.name.endswith(".state.json"):
            continue
        try:
            saved = json.loads(plan_file.read_text())
        except ValueError:
            continue
        root = saved.get("project", "")
        if project in (plan_file.stem, os.path.basename(root)) and os.path.isdir(root):
            return os.path.realpath(root)
    raise ValueError(f"unknown project {project!r} — use list_projects, or pass the project's path")


@server.tool(annotations=READ)
@_quiet
def list_projects() -> list[dict]:
    """Projects that have a run plan: name, path, tasks done/total, and whether a run is live."""
    out = []
    for plan_file in sorted(runctl.RUNS.glob("*.json")):
        if plan_file.name.endswith(".state.json"):
            continue
        try:
            root = json.loads(plan_file.read_text()).get("project", "")
        except ValueError:
            continue
        if not os.path.isdir(root):
            continue
        st = runctl.status(runctl.load_plan(root))
        out.append({"name": plan_file.stem, "path": root, "done": st["done"], "total": st["total"],
                    "running": st["running"]})
    return out


@server.tool(annotations=READ)
@_quiet
def show_plan(project: str) -> dict:
    """The project's run plan with defaults filled in: models and where each pass runs,
    workers, time caps, attempts, RunPod budget."""
    plan = runctl.load_plan(resolve(project))
    return {**plan, "problems": runctl.validate(plan)}


@server.tool(annotations=READ)
@_quiet
def run_status(project: str) -> dict:
    """Live status: tasks done/total, whether a run is live, current pass, process counts,
    recent monitor alerts and lines, RunPod pods and spend against budget."""
    return runctl.status(runctl.load_plan(resolve(project)))


@server.tool(annotations=READ)
@_quiet
def recent_activity(project: str, lines: int = 40) -> str:
    """The last lines of the run's monitor log (task starts, passes, skips, alerts)."""
    log = Path(resolve(project), "logs", "monitor.log")
    if not log.exists():
        return "(no monitor log yet)"
    text = re.sub(r"\x1b\[[0-9;]*m", "", log.read_text(errors="ignore"))
    kept = [l for l in text.splitlines() if l.strip()]
    return "\n".join(kept[-max(1, min(lines, 400)):])


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
@_quiet
def preflight(project: str, reference: str = "", mutants: bool = True) -> str:
    """Check the project's kit before a run: finished gates pass, task size, context coverage,
    ordering. With `reference` (a directory mirroring the project with a reference solution):
    also build order and weak gates — mistakes a worker could merge green. Writes
    logs/preflight-<time>.md in the project and returns the report."""
    import preflight as pf
    root = resolve(project)
    st = pf.static_checks(root)
    ref = None
    if reference:
        from sov.refcheck import simulate
        ref = simulate(root, os.path.realpath(os.path.expanduser(reference)), mutate=mutants,
                       log=lambda m: print(m, file=sys.stderr))
    text = pf.report(root, st, None, ref)
    out = Path(root) / "logs" / f"preflight-{__import__('datetime').datetime.now():%Y%m%d-%H%M}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text + "\n")
    return text + f"\n\n→ {out}"


@server.tool(annotations=ACT)
@_quiet
def launch_run(project: str, cheap_cap_minutes: int = 0) -> dict:
    """Start a run (sweep + monitor, plus the pod guard when a pass uses RunPod), using the
    saved plan. Goes through the safe sequence: stops stale watchers, parks the repo clean,
    and REFUSES on uncommitted changes, a live run, or finished tasks failing their gates.
    cheap_cap_minutes overrides the cheap pass's time cap for this run only (not saved)."""
    plan = runctl.load_plan(resolve(project))
    if cheap_cap_minutes:
        plan["cheap"]["cap_minutes"] = int(cheap_cap_minutes)
    messages: list[str] = []
    result = runctl.launch(plan, log=messages.append)
    return {**result, "log": messages}


@server.tool(annotations=ACT)
@_quiet
def stop_run(project: str, stop_pods: bool = True) -> dict:
    """Stop the run: watchers first, then sweep/supervisor/workers; forgive the in-flight
    task's strike; park the repo clean; stop the run's RunPod pods unless stop_pods is false.
    Finished work stays committed."""
    return runctl.stop(runctl.load_plan(resolve(project)), stop_pods=stop_pods)


if __name__ == "__main__":
    server.run("stdio")
