"""Escalation: escalate.md, Claude escalation (normal and chronic), the
tier2 queue for --deep runs, and the incomplete-task report.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from datetime import date
import json
import os
import re as _re
from sov.config import (
    CLAUDE_MODEL,
    PHASE_STRIKE_LIMIT,
    TIER_MODELS,
)
from sov import state
from sov.files import LOCKED_FILES
from sov.lazytools import _get_advisor
from sov.llm import _extract_first_json_object
from sov.telemetry import VELOCITY_LOG
from sov.ui import DIM, RED, RESET, YELLOW


# After this many attempts with a structurally-unfixable error, stop burning
# retries and escalate to Claude instead.
ESCALATE_AFTER = 3

# If a task appears in velocity.jsonl with outcome "skipped" or "failed" this many
# times, bypass all local model tiers and send it straight to Claude.  Set to 0
# to disable (every task starts at tier 1 every run regardless of history).
CHRONIC_BLOCKER_THRESHOLD = int(os.getenv("CHRONIC_THRESHOLD", "1"))


def _write_escalation(project_root: str, task_idx: int, task: str,
                      attempt: int, error: str,
                      written: list[str], skipped: list[str],
                      is_test: bool = False, identical_error: bool = False) -> str:
    """Write a rich escalation report for Claude to act on. Returns the path."""
    path = os.path.join(project_root, state.ESCALATE_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Categorise the likely cause
    if _re.search(r'uri_does_not_exist', error):
        cause = "Missing package — `flutter pub get` may not have been run, or package absent from pubspec.yaml."
    elif _re.search(r'annotate_overrides|field_shadows_inherited', error):
        cause = "Pre-existing lint in a model file (star.dart, mote.dart, etc.) unrelated to this task."
    elif not written:
        cause = "All candidate files are LOCKED — 35B produced correct code but had nowhere to write it."
    elif is_test and identical_error:
        # Observed directly (2026-09-02, old-car-radio): a widget test kept
        # failing with the byte-identical assertion result across every tier
        # the ladder tried, including genuinely different models where the
        # project's config offered one. The root cause was a broken gesture
        # simulation in the test itself (a drag whose start/end points both
        # sat on an atan2 discontinuity, so the angle could never change) —
        # no amount of retrying the IMPLEMENTATION could ever pass an
        # assertion that the TEST'S OWN math made unreachable. An identical
        # failure surviving a real model change on a test task is a much
        # stronger "the spec is wrong" signal than on a feature task (where
        # it usually just means the model can't find the right API).
        cause = (
            "Repeated IDENTICAL failure across every tier tried (including a "
            "genuinely different model, where the config offered one) on a "
            "TEST task. This pattern usually means the test's own approach "
            "is broken, not the implementation — retrying further is "
            "unlikely to help. Read the test file and the code it exercises "
            "directly before deciding whether to fix the test or the code."
        )
    else:
        cause = "Repeated identical error across attempts — likely a structural API mismatch."

    with open(path, "w") as f:
        f.write(f"# Escalation — Task {task_idx}\n\n")
        f.write(f"**Task:** {task}\n\n")
        f.write(f"**Escalated after:** {attempt} attempt(s)\n\n")
        f.write(f"**Likely cause:** {cause}\n\n")
        if skipped:
            f.write(f"**Skipped (locked):** `{'`, `'.join(skipped)}`\n\n")
        if written:
            f.write(f"**Last written:** `{'`, `'.join(written)}`\n\n")
        f.write(f"## Validation error\n```\n{error[-3000:]}\n```\n\n")
        f.write(f"## Resume after fix\n")
        f.write(f"```\n./work --project . --start-at {task_idx}\n```\n")
    return path


def _escalate_to_claude(
    task: str, file_contents: dict, errors: str,
    project_root: str, is_test: bool,
) -> tuple[dict, bool]:
    """Call Claude API as the final escalation tier.

    Returns (changes, success) where changes is the same {path: content} dict
    that implement_task returns, and success=True if validation passed.
    """
    try:
        import anthropic
    except ImportError:
        print(f"  {YELLOW}anthropic package not installed — pip install anthropic{RESET}")
        return {}, False

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print(f"  {YELLOW}ANTHROPIC_API_KEY not set — skipping Claude escalation{RESET}")
        return {}, False

    vision = open("VISION.md").read() if os.path.exists("VISION.md") else ""
    _project_rules = open(".roorules").read() if os.path.exists(".roorules") else ""
    _global_rules_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "global_rules.md")
    _global_rules = open(_global_rules_path).read() if os.path.exists(_global_rules_path) else ""
    rules = (_global_rules + "\n\n---\n\n# Project-specific rules\n\n" + _project_rules).strip()
    context = "\n\n".join(f"=== {p} ===\n{c}" for p, c in file_contents.items()
                          if p not in LOCKED_FILES)
    locked_dirs: dict[str, list[str]] = {}
    for f in sorted(LOCKED_FILES):
        d = f.rsplit("/", 1)[0] if "/" in f else "root"
        locked_dirs.setdefault(d, []).append(f.rsplit("/", 1)[-1])
    locked_list = "\n".join(
        f"  {d}/: {', '.join(names)}" for d, names in sorted(locked_dirs.items())
    )

    system_prompt = (
        "You are an expert Dart/Flutter coding agent acting as final escalation. "
        "Previous smaller models failed. Apply precise, minimal fixes.\n"
        "Return ONLY a JSON object where keys are file paths and values are COMPLETE file contents.\n"
        "No explanation, no markdown outside the JSON.\n\n"
        f"LOCKED files (do not return these):\n{locked_list}\n\n"
        f"Project rules:\n{rules}"
    )
    user_prompt = (
        f"Task: {task}\n\n"
        f"Vision:\n{vision[:1000]}\n\n"
        f"Current files:\n{context}\n\n"
        f"All previous attempts failed with these errors — study them carefully:\n{errors}"
    )

    print(f"  {DIM}☁  Escalating to Claude ({CLAUDE_MODEL})...{RESET}", end="", flush=True)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw = msg.content[0].text if msg.content else ""
        if "{" not in raw:
            print(f" {YELLOW}no JSON in response{RESET}")
            return {}, False
        changes = _extract_first_json_object(raw)
        if not changes:
            print(f" {YELLOW}could not parse JSON from Claude response{RESET}")
            return {}, False
        print(f" {DIM}done ({len(changes)} file(s)){RESET}")
        return changes, True
    except Exception as exc:
        print(f" {RED}Claude API error: {exc}{RESET}")
        return {}, False


def _count_prior_skips(task: str, project_root: str) -> int:
    """Return how many previous runs skipped or failed this exact task.

    Matches on the first 120 chars of task text (same truncation used when
    writing velocity records).  "budget" counts too — those are timeout skips.
    """
    vel_path = os.path.join(project_root, VELOCITY_LOG)
    if not os.path.exists(vel_path):
        return 0
    task_key = task[:120]
    count = 0
    with open(vel_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("task", "")[:120] == task_key:
                    if rec.get("outcome") in ("skipped", "budget", "failed"):
                        count += 1
            except Exception:
                pass
    return count


def _escalate_to_claude_chronic(
    task: str, file_contents: dict, prior_skips: int,
    project_root: str, is_test: bool,
) -> tuple[dict, bool]:
    """Claude escalation for chronic blockers — richer prompt, asks for a rule.

    Unlike the standard _escalate_to_claude() call (which passes accumulated
    errors from the current run), this version sends a clean, focused prompt
    that tells Claude exactly why it's been called: the task has failed in
    previous runs and local models consistently cannot solve it.

    Also asks Claude to propose a .roorules entry that would prevent this class
    of error from recurring, which is appended to logs/rule_drafts.jsonl.
    Returns (changes, success).
    """
    try:
        import anthropic
    except ImportError:
        print(f"  {YELLOW}anthropic package not installed — pip install anthropic{RESET}")
        return {}, False

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {}, False

    vision = open("VISION.md").read() if os.path.exists("VISION.md") else ""
    _project_rules = open(".roorules").read() if os.path.exists(".roorules") else ""
    _global_rules_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "global_rules.md")
    _global_rules = open(_global_rules_path).read() if os.path.exists(_global_rules_path) else ""
    rules = (_global_rules + "\n\n---\n\n# Project-specific rules\n\n" + _project_rules).strip()
    context = "\n\n".join(
        f"=== {p} ===\n{c}" for p, c in file_contents.items()
        if p not in LOCKED_FILES
    )

    # Pull the last known error for this task from velocity log for extra context
    last_errors = ""
    try:
        adv = _get_advisor()
        top = adv.top_error_patterns(project_root, n=5)
        if top:
            last_errors = "\n".join(
                f"  - {p['code']} ({p['count']}× in this project)" for p in top
            )
    except Exception:
        pass

    locked_dirs: dict[str, list[str]] = {}
    for f in sorted(LOCKED_FILES):
        d = f.rsplit("/", 1)[0] if "/" in f else "root"
        locked_dirs.setdefault(d, []).append(f.rsplit("/", 1)[-1])
    locked_list = "\n".join(
        f"  {d}/: {', '.join(names)}" for d, names in sorted(locked_dirs.items())
    )

    system_prompt = (
        "You are an expert Dart/Flutter coding agent acting as the final escalation tier.\n"
        "This task is a CHRONIC BLOCKER — smaller models have failed it in previous runs.\n"
        "You have ONE attempt. Apply a precise, minimal, correct fix.\n\n"
        "Return a JSON object with TWO keys:\n"
        '  "files": { "<path>": "<complete file content>", ... }  — the code changes\n'
        '  "new_rule": "<one paragraph rule>"  — a .roorules entry that would prevent '
        "this class of error from recurring (empty string if nothing to add)\n\n"
        f"LOCKED files (never return these in 'files'):\n{locked_list}\n\n"
        f"Project coding rules:\n{rules}"
    )
    user_prompt = (
        f"CHRONIC BLOCKER: this task has been skipped {prior_skips} previous run(s).\n"
        f"Task: {task}\n\n"
        f"Vision:\n{vision[:800]}\n\n"
        + (f"Most frequent errors in this project (avoid these patterns):\n{last_errors}\n\n"
           if last_errors else "")
        + f"Current file contents:\n{context}"
    )

    print(
        f"  {DIM}☁  Chronic blocker → Claude ({CLAUDE_MODEL}) directly "
        f"(skipped {prior_skips}× before)...{RESET}",
        end="", flush=True,
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw = msg.content[0].text if msg.content else ""
        if "{" not in raw:
            print(f" {YELLOW}no JSON in response{RESET}")
            return {}, False
        parsed = _extract_first_json_object(raw)
        if not parsed:
            print(f" {YELLOW}could not parse JSON from Claude response{RESET}")
            return {}, False
        files = parsed.get("files", {})
        new_rule = parsed.get("new_rule", "").strip()
        print(f" {DIM}done ({len(files)} file(s)){RESET}")

        # Persist any new rule Claude suggests
        if new_rule:
            import hashlib as _hl3
            rules_path = os.path.join(project_root, "logs", "rule_drafts.jsonl")
            os.makedirs(os.path.dirname(rules_path), exist_ok=True)
            existing: set[str] = set()
            if os.path.exists(rules_path):
                with open(rules_path) as f:
                    for line in f:
                        try:
                            existing.add(json.loads(line).get("rule", "")[:80])
                        except Exception:
                            pass
            if new_rule[:80] not in existing:
                with open(rules_path, "a") as f:
                    f.write(json.dumps({
                        "rule":     new_rule,
                        "task":     task[:120],
                        "date":     date.today().isoformat(),
                        "source":   "claude_chronic",
                    }) + "\n")
                print(f"  {DIM}📝 Claude suggested a new .roorules entry (logged){RESET}")

        return files, bool(files)
    except Exception as exc:
        print(f" {RED}Claude API error: {exc}{RESET}")
        return {}, False


class EscalationNeeded(Exception):
    """Raised when a task hits a structural error that all tiers cannot fix."""
    def __init__(self, task_idx: int, path: str):
        self.task_idx = task_idx
        self.path = path


TIER2_QUEUE_FILE = "logs/tier2_queue.jsonl"

def _write_tier2_queue(project_root: str, task_idx: int, task: str,
                       error_types: list[str]) -> None:
    """Append a task to the tier2 queue file so a --deep run can pick it up."""
    path = os.path.join(project_root, TIER2_QUEUE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        json.dump({
            "task_idx":    task_idx,
            "task":        task,
            "queued_date": date.today().isoformat(),
            "error_types": error_types,
        }, f)
        f.write("\n")


# ─── Incomplete-task report ───────────────────────────────────────────────────

def _write_incomplete_report(
    project_root: str,
    skipped: list[tuple[int, str]],
    log_file: str,
) -> None:
    """Write logs/incomplete_tasks.md with skipped tasks and a recovery plan."""
    os.makedirs(os.path.join(project_root, "logs"), exist_ok=True)
    report_path = os.path.join(project_root, "logs", "incomplete_tasks.md")

    tier_desc = " → ".join(f"`{m}`" for m in TIER_MODELS)
    lines = [
        "# Incomplete Tasks — Recovery Plan\n",
        f"Generated after work run. {len(skipped)} task(s) skipped because all "
        f"{len(TIER_MODELS)} model tiers ({tier_desc}) produced the same error "
        f"{PHASE_STRIKE_LIMIT}× each.\n",
        "\n## Skipped Tasks\n",
    ]
    for idx, task in skipped:
        lines.append(f"- [ ] **Task {idx}:** {task}\n")

    lines += [
        "\n## How to Resume\n",
        "1. Read the errors in `logs/errors.jsonl` for each skipped task index.\n",
        "2. Identify the root cause (usually a missing file, wrong import, or API mismatch).\n",
        "3. Apply a manual fix to unblock the task.\n",
        "4. Resume from the first skipped task:\n",
        "   ```bash\n",
        f"   python work.py --project {project_root} --start-at {skipped[0][0]}\n",
        "   ```\n",
        "\n## Common Fixes\n",
        "- **undefined_class**: Find where the class is defined and add the import.\n",
        "- **wrong API**: Check `.roorules` for the correct method name.\n",
        "- **locked file conflict**: The task needs a file that is locked — "
        "consider unlocking it or pre-building the required change.\n",
        "\n## Skipped Task Indices (for --start-at)\n",
        ", ".join(str(idx) for idx, _ in skipped) + "\n",
    ]

    with open(report_path, "w") as f:
        f.writelines(lines)

    print(f"\n  {YELLOW}📋 Incomplete tasks report: {report_path}{RESET}")
    print(f"  {YELLOW}Skipped task indices: "
          f"{', '.join(str(i) for i, _ in skipped)}{RESET}")
