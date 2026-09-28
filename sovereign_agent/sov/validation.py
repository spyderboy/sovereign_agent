"""Validation: baseline error snapshots, the project validate commands, the
per-task gate, and parking tasks for human review.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from datetime import date
import os
import re
import shutil
import subprocess
from sov import state
from sov.telemetry import REVIEW_LOG


# ─── Validation ───────────────────────────────────────────────────────────────

def _extract_flutter_errors(output: str) -> frozenset[str]:
    """Return the set of 'error •' lines from flutter analyze output."""
    return frozenset(
        l.strip() for l in output.splitlines()
        if re.match(r"\s*error\s*•", l)
    )


def snapshot_baseline_errors(project_root: str) -> frozenset[str]:
    """Run flutter analyze before any coding and capture the pre-existing errors.

    These are errors the model didn't cause and cannot fix — they must not
    block task validation.
    """
    if state.PROJECT_VALIDATE_COMMANDS:
        # Per-project gates are absolute (a Go build either passes or it
        # doesn't) — no flutter baseline to snapshot.
        return frozenset()
    if not shutil.which("flutter"):
        return frozenset()
    try:
        result = subprocess.run(
            ["flutter", "analyze", "--no-fatal-infos", "--no-fatal-warnings"],
            capture_output=True, text=True, timeout=120,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr).strip()
        return _extract_flutter_errors(output)
    except Exception:
        return frozenset()


def _park_for_review(project_root: str, task_idx: int, task: str, model: str,
                     written: list[str], foreign: list[str], output: str) -> None:
    """Append a human-readable diagnosis to logs/needs_review.md."""
    path = os.path.join(project_root, REVIEW_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path)
    with open(path, "a") as f:
        if fresh:
            f.write("# Tasks parked for review\n\n"
                    "Each entry failed repeatedly with errors naming no file the\n"
                    "task wrote. A larger model cannot fix these — the cause is an\n"
                    "earlier merge or a gate that cannot pass.\n")
        f.write(f"\n---\n\n## {date.today().isoformat()} — task {task_idx}\n\n")
        f.write(f"**Task:** {task[:300]}\n\n")
        f.write(f"**Last model:** {model}\n\n")
        f.write(f"**This task wrote:** {', '.join(written) or '(nothing)'}\n\n")
        f.write(f"**Errors named these instead:** {', '.join(foreign[:8])}\n\n")
        f.write("**Likely cause:** a file merged by an earlier task is wrong, or\n"
                "this task's gate cannot pass on the current tree. Check whether\n"
                "the blamed file was gated on `analyze` only — that proves it\n"
                "compiles, not that it is correct.\n\n")
        f.write("<details><summary>Validator output</summary>\n\n```\n")
        f.write(output[-2000:].rstrip() + "\n```\n\n</details>\n")


def validate(
    baseline_errors: frozenset[str] | None = None,
    files_written: list[str] | None = None,
    task_gate: str | None = None,
) -> tuple[bool, str]:
    # ── Per-project gates (from .sovereign_config.json "validate_commands") ──
    # Run in order, cwd = project root (main() chdirs there at startup).
    # First nonzero exit fails the task with that command's output.
    if state.PROJECT_VALIDATE_COMMANDS:
        for shell_cmd in state.PROJECT_VALIDATE_COMMANDS:
            try:
                result = subprocess.run(
                    ["bash", "-lc", shell_cmd],
                    capture_output=True, text=True, timeout=300,
                )
            except subprocess.TimeoutExpired:
                return False, f"$ {shell_cmd}\n(validator timed out after 300 s)"
            if result.returncode != 0:
                output = (result.stdout + result.stderr).strip()
                return False, f"$ {shell_cmd}\n{output[-3000:]}"
        # ── Per-task acceptance gate ──────────────────────────────────────
        # validate_commands passing only means the code TYPE-CHECKS and leaves
        # locked files intact — a stub satisfies both. The task's own gate is
        # what proves the behaviour exists. Run it last so a stub cannot pass.
        if task_gate:
            try:
                result = subprocess.run(
                    ["bash", "-lc", task_gate],
                    capture_output=True, text=True, timeout=300,
                )
            except subprocess.TimeoutExpired:
                return False, f"$ {task_gate}\n(task gate timed out after 300 s)"
            if result.returncode != 0:
                output = (result.stdout + result.stderr).strip()
                return False, f"$ {task_gate}\n{output[-3000:]}"
        return True, "all project validate_commands + task gate passed"

    for cmd, label in [
        (["flutter", "analyze", "--no-fatal-infos", "--no-fatal-warnings"], "flutter analyze"),
        (["python", "-m", "pytest", "--tb=short", "-q"], "pytest"),
        (["npm",    "test",    "--", "--watchAll=false"], "npm test"),
    ]:
        if not shutil.which(cmd[0]):
            continue
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stdout + result.stderr).strip()
        if cmd[0] == "flutter":
            # Flutter exits with code 1 even for plugin deprecation warnings.
            # We only fail if NEW error • lines appear that weren't in the
            # pre-task baseline AND are in files the worker actually wrote.
            # Errors in unrelated files (generated by other workers on other
            # branches) must not block this task — they're not our responsibility.
            current_errors = _extract_flutter_errors(output)
            new_errors = current_errors - (baseline_errors or frozenset())
            if files_written:
                # Narrow new_errors to only those referencing written files.
                # An error line looks like: "error • <msg> • lib/foo/bar.dart:10:5 • code"
                # We match on the file path portion.
                def _in_written(err_line: str) -> bool:
                    return any(
                        f.replace("\\", "/") in err_line.replace("\\", "/")
                        for f in files_written
                    )
                new_errors = frozenset(e for e in new_errors if _in_written(e))
            if not new_errors:
                return True, output[-3000:]
            # Build an output that highlights only the relevant errors
            new_err_text = "\n".join(sorted(new_errors))
            return False, new_err_text + "\n\n" + output[-2000:]
        return result.returncode == 0, output[-3000:]
    return True, "No validator found — skipping"
