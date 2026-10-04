"""Run telemetry: velocity, traces, race log, and the failure ledger.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from datetime import date
import json
import os
import re
import re as _re
import time


# ─── Task runner ─────────────────────────────────────────────────────────────

VELOCITY_LOG   = "logs/velocity.jsonl"
REVIEW_LOG     = "logs/needs_review.md"
TRACES_LOG     = "logs/task_traces.jsonl"
RACE_LOG       = "logs/race.jsonl"

# Errors the 35B cannot fix by rewriting code:
#   - locked files (no files written repeatedly)
#   - missing package / flutter pub get not run
#   - pre-existing lint in a file the task doesn't touch
#   - field shadows inherited (model API contract issue)
STRUCTURAL_PATTERNS = [
    r'uri_does_not_exist',
    r'annotate_overrides',
    r'field_shadows_inherited',
    r'concrete_class_has_enum_superinterface',
    r'extends_non_class',          # model hallucinated a class hierarchy — hints fix this faster
    r'non_type_as_type_argument',  # same root cause as extends_non_class
]

def _is_structural(error_output: str) -> bool:
    return any(_re.search(p, error_output, _re.IGNORECASE)
               for p in STRUCTURAL_PATTERNS)


def _extract_error_codes(output: str) -> list[str]:
    """Pull flutter analyze error codes from output."""
    return list(dict.fromkeys(re.findall(r'\b([a-z][a-z0-9_]{3,})\b', output)))[:10]


def task_sha(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.strip().encode()).hexdigest()[:12]


_HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_harness_version: str | None = None


def harness_version() -> str:
    """Xanadu's own git commit, '-dirty' with uncommitted tracked changes. Once per process."""
    global _harness_version
    if _harness_version is None:
        import subprocess
        try:
            sha = subprocess.run(["git", "-C", _HARNESS_ROOT, "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            dirty = subprocess.run(["git", "-C", _HARNESS_ROOT, "status", "--porcelain", "--untracked-files=no"],
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            _harness_version = (sha or "unknown") + ("-dirty" if dirty else "")
        except Exception:
            _harness_version = "unknown"
    return _harness_version


def _count_lines(path: str) -> int:
    try:
        with open(path, errors="ignore") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _distinct_error_codes(path: str) -> int:
    """errors.jsonl has a line per failed attempt; a learned pattern is a distinct code."""
    codes = set()
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    codes.update(json.loads(line).get("error_codes") or ())
                except (ValueError, AttributeError, TypeError):
                    continue
    except OSError:
        pass
    return len(codes)


def provenance(project_root: str) -> dict:
    """What ran the task (harness), what Xanadu had learned by itself (learned), and a
    fingerprint of what humans had written (human) — 2026-10-04, so 'Xanadu improves
    with use' can be told apart from model upgrades and human fixes.

    learned: mined guards (origin "mined"), distinct error codes seen, drafted rules, routing strikes.
    human:   hand-written guards, and a hash of .roorules + global_rules.md + error hints
             + hand-written guards — it changes only when a person edits them."""
    import hashlib
    try:
        cfg = json.load(open(os.path.join(project_root, ".sovereign_config.json")))
    except (OSError, ValueError):
        cfg = {}
    guards = cfg.get("additional_bad_patterns") or []
    mined = [g for g in guards if isinstance(g, dict) and g.get("origin") == "mined"]
    hand = [g for g in guards if not (isinstance(g, dict) and g.get("origin") == "mined")]
    try:
        strikes = len(json.load(open(os.path.join(project_root, "logs", "strong_failures.json"))))
    except (OSError, ValueError, TypeError):
        strikes = 0
    h = hashlib.sha1()
    for path in (os.path.join(project_root, ".roorules"), os.path.join(_HARNESS_ROOT, "global_rules.md")):
        try:
            h.update(open(path, "rb").read())
        except OSError:
            pass
        h.update(b"\0")
    h.update(json.dumps([hand, cfg.get("additional_error_hints")], sort_keys=True).encode())
    return {
        "harness": harness_version(),
        "learned": {"guards_mined": len(mined),
                    "error_patterns": _distinct_error_codes(os.path.join(project_root, "logs", "errors.jsonl")),
                    "rule_drafts": _count_lines(os.path.join(project_root, "logs", "rule_drafts.jsonl")),
                    "strikes": strikes},
        "human": {"guards": len(hand), "rules": h.hexdigest()[:8]},
    }


def _endpoint(model: str) -> str:
    from sov.config import endpoint_for
    return endpoint_for(model or "")


def where_it_ran(model: str | None) -> dict:
    """When and where (2026-10-04): exact UTC time for unattended stretches and stalls, and the
    machine class the model ran on — this Mac vs a RunPod GPU — so speed and cost compare."""
    from datetime import datetime, timezone
    from urllib.parse import urlparse
    host = (urlparse(_endpoint(model or "")).hostname or "").lower()
    where = ("local" if host in ("", "localhost", "127.0.0.1", "::1") else
             "runpod" if "runpod" in host else "remote")
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "where": where}
    if where == "runpod" and os.getenv("XANADU_GPU"):
        out["gpu"] = os.getenv("XANADU_GPU")
    return out


def _append_velocity(project_root: str, record: dict):
    from sov import state
    cur = state.CURRENT_TASK
    if cur and "task_sha" not in record and cur.startswith((record.get("task") or "\0").rstrip()):
        record = {**record, "task_sha": task_sha(cur)}
    if "harness" not in record:
        try:
            record = {**record, **provenance(project_root)}
        except Exception:                              # telemetry must never stop a task
            pass
    if "at" not in record:
        try:
            record = {**record, **where_it_ran(record.get("model"))}
        except Exception:
            pass
    path = os.path.join(project_root, VELOCITY_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _append_race(project_root: str, record: dict):
    """Append one race-attempt record to race.jsonl for 4B vs 7B analysis."""
    path = os.path.join(project_root, RACE_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _append_trace(project_root: str, record: dict):
    """Append one record to task_traces.jsonl for QLoRA fine-tuning data collection."""
    path = os.path.join(project_root, TRACES_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _log_attempt_trace(
    project_root: str, task_idx: int, task: str, attempt: int,
    tier: int, model: str, is_test: bool, rel_files: list[str],
    call_info: dict, written: list[str], bad_patterns: bool,
    passed: bool, output: str, errors_fed: str,
    autofix_resolved: bool, task_start: float,
) -> None:
    """Log one model-call attempt — captures the full (prompt → output → result) triple.

    Record types:
      "attempt" — one per model call that reached validation.
                  system_prompt + user_prompt + raw_output form the training pair.
                  validation_passed=True + autofix_resolved flag identifies clean positives.
                  errors_fed_in shows what failure context was injected (useful for DPO).
    """
    _append_trace(project_root, {
        "record_type":       "attempt",
        "trace_id":          f"{task_idx}:{attempt}",
        "task_idx":          task_idx,
        "task":              task,
        "date":              date.today().isoformat(),
        "attempt":           attempt,
        "tier":              tier,
        "model":             model,
        "is_test":           is_test,
        "relevant_files":    rel_files,
        "system_prompt":     call_info.get("system", ""),
        "user_prompt":       call_info.get("user", ""),
        "raw_output":        call_info.get("raw", "")[:8000],  # cap — 35B can be verbose
        "files_written":     written,
        "bad_patterns_blocked": bad_patterns,
        "validation_passed": passed,
        "autofix_resolved":  autofix_resolved,
        "validation_output": output[-1500:],
        "errors_fed_in":     errors_fed[:1500],  # what failure context the model saw
        "elapsed_s":         round(time.time() - task_start, 1),
    })


def _log_task_summary(
    project_root: str, task_idx: int, task: str,
    task_outcome: str, total_attempts: int, model: str, task_start: float,
) -> None:
    """Log a task-level summary record (one per task, any outcome).

    task_outcome: "done" | "skipped" | "failed" | "budget" | "timeout" | "no_output"
    Joins to attempt records via task_idx for training dataset construction.
    """
    _append_trace(project_root, {
        "record_type":    "task_summary",
        "trace_id":       f"{task_idx}:summary",
        "task_idx":       task_idx,
        "task":           task,
        "date":           date.today().isoformat(),
        "task_outcome":   task_outcome,
        "total_attempts": total_attempts,
        "final_model":    model,
        "duration_s":     round(time.time() - task_start, 1),
    })
    if task_outcome != "done":
        _ledger(project_root, task_idx, model,
                f"task_{task_outcome}", task[:120])


# ─── Failure ledger ──────────────────────────────────────────────────────────
# One TSV line per failure event: answers "are we still making the same kind
# of mistakes?" with `cut -f4 logs/failure_ledger.tsv | sort | uniq -c`
# instead of transcript archaeology. Lives in logs/ (excluded from git clean).

FAILURE_LEDGER = "logs/failure_ledger.tsv"

# Ordered most-specific first; first match wins.
#
# Until 2026-07-25 every pattern here was Go-flavoured (`undefined:`, `gofmt`,
# `mismatched types`). Against Dart analyzer or tsc output none of them match,
# so the real 76-row ledger from the Dart project categorised 76/76 as "other"
# — the instrument that answers "are we still making the same mistakes?" read
# zero on the project generating all the mistakes. Patterns below are grouped
# by language but checked against all output: error text is distinctive enough
# that cross-language false matches are not a practical concern, and a single
# combined list keeps categories comparable across projects.
_LEDGER_CATEGORIES = [
    # ── Agent-internal gates (language-independent) ──────────────────────
    (r"UNKNOWN IDENTIFIER|UNKNOWN IMPORT",                       "hallucinated_api"),
    (r"SCOPE VIOLATION|frozen",                                  "scope_violation"),
    (r"INVALID FORMAT|patch descriptor",                         "bad_output_format"),
    (r"PREFLIGHT|pub get has not been run|node_modules is absent","environment"),

    # ── Truncated / malformed output ─────────────────────────────────────
    (r"unexpected EOF|expected '\}'|expected declaration|"
     r"expected_token|missing_identifier|unexpected_token|"
     r"expected_executable|missing_function_body|"
     r"missing_function_parameters",                             "truncated_output"),

    # ── Wrong-language syntax ────────────────────────────────────────────
    # Models reach for another stack's grammar: Swift/Kotlin `as?` in Dart,
    # keywords used as identifiers, abstract-member confusion. Distinct from
    # truncation — the output is complete, just not this language.
    (r"SYNTAX ERROR|can't be used as an identifier because it's a keyword|"
     r"must have a method body because|"
     r"expected_identifier_but_got_keyword|"
     r"IndentationError|SyntaxError",                            "wrong_language_syntax"),

    # ── Unresolved imports — the largest single class on Dart (146×) ─────
    (r"uri_does_not_exist|Target of URI doesn't exist|"
     r"URI_DOES_NOT_EXIST|Cannot find module|TS2307|"
     r"ModuleNotFoundError|No module named",                     "unresolved_import"),

    # ── Invented symbols ─────────────────────────────────────────────────
    (r"undefined:|has no field or method|undeclared name|"
     r"undefined_function|undefined_identifier|undefined_class|"
     r"undefined_method|undefined_named_parameter|"
     r"isn't defined for the type|isn't a function|"
     r"TS2304|TS2339|TS2551|"
     r"NameError|AttributeError",                                "undefined_identifier"),

    # ── Type errors ──────────────────────────────────────────────────────
    (r"cannot use .* as .* value|type mismatch|mismatched types|"
     r"invalid_assignment|argument_type_not_assignable|"
     r"non_type_as_type_argument|cast_to_non_type|extends_non_class|"
     r"return_of_invalid_type|unchecked_use_of_nullable_value|"
     r"TS2322|TS2345|TS2554|"
     r"is not assignable to",                                    "type_error"),

    # ── Override / signature contract ────────────────────────────────────
    (r"override_on_non_overriding_member|"
     r"super_formal_parameter_without_associated_positional|"
     r"not_enough_positional_arguments|"
     r"missing_default_value_for_parameter|"
     r"missing_required_argument",                               "signature_error"),

    # ── Unused declarations ──────────────────────────────────────────────
    (r"imported and not used|declared and not used|"
     r"unused_local_variable|unused_import|TS6133|"
     r"is declared but its value is never read",                 "unused_decl"),

    # ── Control-flow / async ─────────────────────────────────────────────
    (r"body_might_complete_normally|await_in_wrong_context|"
     r"missing_return|await is only valid",                      "control_flow"),

    # ── Test failures ────────────────────────────────────────────────────
    (r"--- FAIL|FAIL\t|test.*failed|Some tests failed|"
     r"Expected:.*Actual:|AssertionError",                       "test_failure"),

    # ── Formatting / lint ────────────────────────────────────────────────
    (r"gofmt|not gofmt'd|file is not formatted|"
     r"dart format|prettier|eslint|"
     r"prefer_const|unnecessary_",                               "format"),
]

# Events whose payload is the task description, not compiler output. Running
# the category regexes over a task title produces confident nonsense — six of
# the ten stubborn "other" rows in the 2026-07-13 ledger were budget events
# being categorised on their own task text.
_NON_ERROR_EVENTS = {"task_budget", "task_no_output", "task_skipped",
                     "preflight_failed"}


def _ledger_category(error_text: str, event: str = "") -> str:
    if event in _NON_ERROR_EVENTS:
        return "-"
    for pat, cat in _LEDGER_CATEGORIES:
        if _re.search(pat, error_text, _re.IGNORECASE):
            return cat
    return "other"

def _ledger(project_root: str, task_idx: int, model: str,
            event: str, error_text: str = "") -> None:
    """Append one failure event. event: attempt_blocked | validate_failed |
    task_skipped | task_failed | task_done_after_retry ..."""
    try:
        path = os.path.join(project_root, FAILURE_LEDGER)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cat = _ledger_category(error_text, event) if error_text else "-"
        # first identifier-ish detail from the error, for grep-ability
        m = _re.search(r"`([^`]+)`|undefined: (\S+)|\"([^\"]+)\"", error_text)
        detail = next((g for g in (m.groups() if m else ()) if g), "")[:80]
        line = "\t".join([
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            str(task_idx), model, event, cat,
            detail.replace("\t", " "),
            error_text[:200].replace("\n", " ").replace("\t", " "),
        ])
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # the ledger must never kill a run
