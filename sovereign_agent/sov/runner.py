"""The retry ladder: run_task drives one task through tiers, attempts, autofix,
the advisor and escalation until it passes, is skipped, or fails.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from datetime import date
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError
from requests.exceptions import ReadTimeout
import import_fixer
import os
import prompt_artifacts
import re
import time
from sov.config import (
    OLLAMA_THINK_RETRIES,
    CLAUDE_MODEL,
    MAX_TASK_SECONDS,
    MODEL_CTX,
    MODEL_PARAMS,
    PHASE_MAX_ATTEMPTS,
    PHASE_STRIKE_LIMIT,
    PLANNER_MODEL,
    QUICK_PARAM_LIMIT_B,
    RACE_ENABLED,
    RACE_MODEL,
    TIER1_MODEL,
    TIER2_MAX_ATTEMPTS,
    TIER4_MAX_ATTEMPTS,
    TIER_MODELS,
)
from sov import state
from sov.budget import compute_task_budget
from sov.contract_use import unused_contract
from sov.deps import _missing_dart_deps, _missing_local_deps, _missing_python_deps, _symbol_index
from sov.escalation import CHRONIC_BLOCKER_THRESHOLD, _count_prior_skips, _escalate_to_claude, _escalate_to_claude_chronic, _write_escalation, _write_tier2_queue
from sov.files import LOCKED_FILES, backup_files, find_relevant_files, read_files, restore_files, update_api_guide, write_changes
from sov.guards import _missing_promised_symbol, _placeholder_body, _unplanned_new_files, apply_error_hints
from sov.lazytools import _get_advisor, _get_autofix, _get_dart_import_fixer
from sov.llm import PromptTooLargeError
from sov.prompting import implement_task
from sov.roadmap import _extract_task_gate, _is_test_task, regression_gates
from sov.telemetry import REVIEW_LOG, _append_race, _append_trace, _append_velocity, _extract_error_codes, _ledger, _log_attempt_trace, _log_task_summary
from sov.ui import DIM, GREEN, RED, RESET, YELLOW
from sov.validation import _park_for_review, snapshot_baseline_errors, validate
from sov.watchdog import heartbeat


def _quick_max_tier_idx(models: list[str], params: dict[str, float], limit_b: float) -> int:
    """Return the exclusive upper bound for --quick mode.

    Scans TIER_MODELS in order and stops at the first model whose total param
    count meets or exceeds limit_b.  The returned index is exclusive, so:
        QUICK_MAX_TIER_IDX = 2  →  tiers 0 and 1 are allowed (tier indices < 2)

    If all models are under the limit, returns len(models) (all tiers allowed).
    Unknown models (not in params dict) are treated as float('inf') — excluded.
    """
    for i, model in enumerate(models):
        if params.get(model, float("inf")) >= limit_b:
            return i
    return len(models)
def _max_attempts_for_tier(idx: int) -> int:
    """Single source of truth for the attempt ceiling — tier 1 / last tier / everything
    in between each get their own budget. Also used for the printed 'Attempt X/N' label,
    which used to always print PHASE_MAX_ATTEMPTS even on tiers enforcing a different cap."""
    if idx == 0:
        return PHASE_MAX_ATTEMPTS
    if idx == len(TIER_MODELS) - 1:
        return TIER4_MAX_ATTEMPTS
    return TIER2_MAX_ATTEMPTS

QUICK_MAX_TIER_IDX: int = _quick_max_tier_idx(TIER_MODELS, MODEL_PARAMS, QUICK_PARAM_LIMIT_B)
MAX_RETRIES    = 50

# Validation failures naming NO file this task wrote, before the task is parked
# for a human instead of climbing the model ladder.
#
# CUMULATIVE, not consecutive. A consecutive rule almost never fires: a worker
# whose file still has its own compile errors interleaves them with the foreign
# gate failures, and every own-file error resets the streak. A gate that failed
# on someone else's file once will fail that way again — the evidence does not
# expire because the next attempt failed differently.
FOREIGN_PARK_LIMIT = 2


def _race_implement(
    task: str, file_contents: dict, errors: str,
    is_test: bool, project_root: str,
) -> tuple[dict, dict, str | None, dict]:
    """Race TIER1_MODEL (4B) vs RACE_MODEL (7B) concurrently.

    Both models are submitted simultaneously to Ollama. The first to return
    non-empty output wins; the loser's result is still collected for stats.
    Returns (changes, call_info, winner_model, race_stats).
    winner_model is None if both models failed.
    """
    import concurrent.futures

    def _run(model_name: str) -> dict:
        t0 = time.time()
        try:
            changes, call_info = implement_task(
                task, file_contents, errors,
                model=model_name, is_test=is_test, project_root=project_root,
            )
            return {"model": model_name, "changes": changes, "call_info": call_info,
                    "time_s": round(time.time() - t0, 2), "ok": bool(changes), "error": None}
        except Exception as exc:
            return {"model": model_name, "changes": {}, "call_info": {},
                    "time_s": round(time.time() - t0, 2), "ok": False, "error": str(exc)}

    results: dict[str, dict] = {}
    winner: dict | None = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_4b = pool.submit(_run, TIER1_MODEL)
        fut_7b = pool.submit(_run, RACE_MODEL)
        for fut in concurrent.futures.as_completed([fut_4b, fut_7b]):
            res = fut.result()
            results[res["model"]] = res
            if res["ok"] and winner is None:
                winner = res   # first valid response wins

    r4 = results.get(TIER1_MODEL, {})
    r7 = results.get(RACE_MODEL, {})
    stats = {
        "4b_model":  TIER1_MODEL,
        "4b_time_s": r4.get("time_s"),
        "4b_ok":     r4.get("ok", False),
        "4b_error":  r4.get("error"),
        "7b_model":  RACE_MODEL,
        "7b_time_s": r7.get("time_s"),
        "7b_ok":     r7.get("ok", False),
        "7b_error":  r7.get("error"),
        "winner":    winner["model"] if winner else None,
    }

    if winner:
        return winner["changes"], winner["call_info"], winner["model"], stats
    return {}, {}, None, stats



# A failed assertion in the task's OWN gate test names only the test file (and
# flutter_test's frames), never the file the task wrote — yet it is exactly this
# task's failure: wrong text, wrong value. Counting it as foreign deferred every
# widget with a logic slip after ONE attempt (magic_task_hat 2026-10-10,
# stats_bar: Found 0 widgets with text "12").
_ASSERTION = re.compile(r"TestFailure|^\s*Expected:|AssertionError|^E\s+assert ", re.M)


def blame_split(output: str, written: list[str], gate_tests: list[str]) -> tuple[list[str], list[str]]:
    """(foreign files the errors name, files of this task they name)."""
    norm = output.replace("\\", "/")
    foreign = sorted({
        m for m in re.findall(r"[\w./-]+\.dart", output)
        if not any(w.replace("\\", "/").endswith(m.lstrip("./"))
                   or m.endswith(w.replace("\\", "/")) for w in written)
        and "/flutter/" not in m and "flutter_test/" not in m and not m.startswith("package:")
    })
    mine = [w for w in written if w.replace("\\", "/") in norm]
    if not mine and _ASSERTION.search(output):
        own = [g for g in gate_tests if g.replace("\\", "/").lstrip("./") in norm]
        if own:
            mine = own
            foreign = [m for m in foreign if not any(m.endswith(g.lstrip("./")) for g in own)]
    return foreign, mine

def _note_needs_split(project_root: str, task: str) -> None:
    path = os.path.join(project_root, "logs", "needs_split.md")
    line = f"- {task.strip()[:160]}"
    try:
        existing = open(path).read()
    except OSError:
        existing = ("# Tasks that look over-scoped\n\nThe model timed out twice without writing "
                    "code. Split each into one obligation per task (see check_task_size.py), "
                    "with exact imports and calls.\n\n")
    if line not in existing:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(existing + line + "\n")


def run_task(task: str, project_root: str, log_file: str,
             task_idx: int = 1,
             budget_s: float | None = None,
             max_tier_idx: int | None = None,
             start_tier_idx: int = 0,
             skip_advisor: bool = False) -> bool | str:
    """Tiered execution: tier1 → tier2 → … → skip.

    max_tier_idx  — exclusive upper bound on TIER_MODELS (None = all tiers).
                    When set, tasks that exhaust max_tier are NOT escalated
                    further; they are returned as "skipped" and the caller
                    is responsible for queuing them for a deeper run.
    start_tier_idx — 0-based index of the first tier to use (default 0 = tier1).
                    Set to 1 for --deep runs that already failed at tier1.

    Each tier gets PHASE_STRIKE_LIMIT consecutive same-error attempts before
    advancing to the next tier. Returns:
      True      — task passed
      False     — all retries exhausted without a stale lock (rare)
      "skipped" — all tiers (up to max_tier_idx) stuck; move to next task
    """
    state.CURRENT_TASK = task
    state.LAST_TASK_DEFERRED = False
    import hashlib as _hl

    _max_tier = max_tier_idx if max_tier_idx is not None else len(TIER_MODELS)

    task_start    = time.time()
    errors_seen: list[str] = []
    foreign_streak = 0
    last_written: list[str] = []
    last_skipped: list[str] = []

    # Tier state — honour start_tier_idx for deep/resume runs
    tier_idx: int  = min(start_tier_idx, _max_tier - 1)
    current_model  = TIER_MODELS[tier_idx]
    phase_stale: int        = 0   # same-error streak within current tier
    silent_timeouts: int    = 0   # timeouts that produced (almost) no code
    phase_attempts: int     = 0   # total validation failures in current tier (catches thrashing)
    consecutive_bad_pattern: int = 0  # bad-pattern blocks in a row; escalate after limit
    last_err_sig   = ""

    is_test = _is_test_task(task)
    call_info: dict = {}    # populated by implement_task on each attempt
    context_trimmed = False  # True after first prompt-too-large trim

    # ── Dependency-abort guard (TS projects) ──────────────────────────────────
    # If the task names sibling project modules that don't exist yet, it can't
    # pass — the worker would just hallucinate the missing symbol and burn every
    # attempt (observed: GameCanvas run before useGameLoop.ts landed → "Cannot
    # find name 'useGameLoop'"). Defer to the retry pass so the dependency can
    # land first, instead of failing the task outright.
    # Runs BEFORE the planner: it needs only the task text, and a deferral
    # after find_relevant_files cost a 27B planner call (~4 min) per blocked
    # task — a strong pass spent 30+ minutes deferring dependents before it
    # reached the blockers themselves (2026-10-02).
    if state.PROJECT_LANGUAGE == "dart":
        _missing = _missing_dart_deps(task, project_root)
    elif state.PROJECT_LANGUAGE in ("typescript", "ts", "tsx"):
        _missing = _missing_local_deps(task, project_root)
    elif state.PROJECT_LANGUAGE == "python":
        _missing = _missing_python_deps(task, project_root)
    else:
        _missing = []
    if True:
        if _missing:
            print(f"  {YELLOW}⏭  Deferred: depends on file(s) not yet present — "
                  f"{', '.join(_missing)}{RESET}")
            print(f"     (will be retried after its dependencies land)")
            state.LAST_TASK_DEFERRED = True
            _append_velocity(project_root, {
                "date": date.today().isoformat(), "task": task[:120],
                "outcome": "skipped", "attempts": 0,
                "duration_s": round(time.time() - task_start, 1),
                "error_types": ["missing_dependency"],
            })
            return "skipped"

    print(f"  {DIM}Finding relevant files ({PLANNER_MODEL})...{RESET}")
    rel_files = find_relevant_files(task, project_root)
    # The test files the gate runs are ALWAYS context: the planner picks by
    # name, and a shared test file (test_jira_v2.py for jira_query_v2.py) was
    # left out, so the worker coded blind against its own gate (2026-10-01).
    # They go FIRST: the context-budget trim below drops from the end, and an
    # appended test was the first file cut (jira_history, same day).
    _gate_tests = [g for g in re.findall(r"([\w./-]+\.(?:py|dart|ts|tsx|go))(?:::[\w\[\]-]+)?",
                                         _extract_task_gate(task) or "")
                   if os.path.exists(os.path.join(project_root, g))]
    rel_files = list(dict.fromkeys(_gate_tests + [f for f in rel_files if f not in _gate_tests]))
    # The task's own target, when it already exists, is ALWAYS context and goes
    # first: the planner left report.html.j2 out of its own two-line include
    # task, so the model rewrote a template it had never seen and SEARCH/REPLACE
    # mode (which needs the target in context) never switched on (2026-10-03).
    _own = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", task)
    if _own and os.path.isfile(os.path.join(project_root, _own.group(1))):
        rel_files = [_own.group(1)] + [f for f in rel_files if f != _own.group(1)]

    # Always-include context (config "context_always_include") plus any
    # spec/*.md files the task text references (e.g. "per spec/03-simulation.md §2").
    spec_refs = re.findall(r"\b[\w./-]*spec/[\w.-]+\.md\b", task)
    # Reference-source context: a PORT task ("port from src/sim/setTarget.ts")
    # names a file in ANOTHER repo the worker can't see, so it guesses APIs. If a
    # mirror of that source exists under reference/ (e.g. reference/ts/src/sim/
    # setTarget.ts), include it so the worker translates instead of inventing.
    ref_refs: list[str] = []
    for _m in re.findall(r"\b(?:src|lib)/[\w./-]+\.(?:ts|tsx|dart|go|py)\b", task):
        for _base in ("reference/ts/", "reference/"):
            _cand = _base + _m
            if os.path.exists(os.path.join(project_root, _cand)):
                ref_refs.append(_cand)
                break
    # Deterministic package context (2026-07-15): the planner sometimes omits
    # the files defining the very types the task must use — game/tick.go
    # never saw ai/ai.go, so every tier died on `undefined: AI`. Force-include
    # the target file's own package dir plus any package referenced as a Go
    # qualifier (`ai.AI`, `game.Tick`) in the task text. sim/ is excluded:
    # its locked core is already always-included and the full package is huge.
    pkg_ctx: list[str] = []
    if state.PROJECT_LANGUAGE == "go":
        _dirs: set[str] = set()
        _tm = re.search(r'\bIn `?([\w./-]+\.go)`?:', task)
        if _tm and os.path.dirname(_tm.group(1)) not in ("", "sim"):
            _dirs.add(os.path.dirname(_tm.group(1)))
        for _pkg in set(re.findall(r'\b([a-z]\w+)\.[A-Z]\w*', task)) - {"sim"}:
            if os.path.isdir(os.path.join(project_root, _pkg)):
                _dirs.add(_pkg)
        for _d in sorted(_dirs):
            _fs = sorted(f for f in os.listdir(os.path.join(project_root, _d))
                         if f.endswith(".go"))[:8]
            pkg_ctx += [os.path.join(_d, f) for f in _fs]
    forced = [f for f in state.PROJECT_ALWAYS_INCLUDE + spec_refs + ref_refs + pkg_ctx
              if os.path.exists(os.path.join(project_root, f))]

    # THE TASK'S OWN TARGET FILE. rel_files is planned ONCE, here, from files
    # that already exist — so a task creating a new file never had that file in
    # its context, and after attempt 1 wrote it, attempt 2 still could not see
    # it. Every attempt therefore started from a blank page and re-rolled every
    # decision that was already right, which is what the error counts show:
    #   arena       12 → 5 → 3 → 1 → 9
    #   hp_pill      5 → 10 → 2 → 5 → 3 → 4
    #   effects      3 → 3 → 9 → 7
    #   effects_beam 2 → 4
    # read_files skips paths that do not exist, so including it unconditionally
    # is a no-op on attempt 1 and the whole point from attempt 2 onward.
    _target_m = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", task)
    if _target_m:
        forced.append(_target_m.group(1))

    # The gate's own tests stay AHEAD of the always-include files: the trim below drops from the end,
    # and with a long context_always_include list the test was the file cut, so the monitor saw
    # workers coding blind against their own gate (magic_task_hat widgets, 2026-10-10).
    rel_files = list(dict.fromkeys(_gate_tests + forced + rel_files))

    # Proactive, size-aware trim (2026-09-09): the reactive PromptTooLargeError
    # path below trims by raw file COUNT (rel_files[:keep]), ignoring actual
    # file sizes — a single large file can blow the budget while five small
    # ones fit easily, so count-based halving either over-trims fine files or
    # under-trims and still overflows, wasting a whole attempt on nothing.
    # Observed directly: nearly every task in the 2026-09-08 old-car-radio run
    # hit "prompt too large" on attempt 1 before any generation happened.
    # Keep files in priority order (forced/target file first, via
    # dict.fromkeys above) and greedily add whole files while the running
    # total stays under budget — same (chars // 3) estimate and 0.85 cutoff
    # ollama()'s own preflight check uses, so this activates BEFORE that
    # check trips, not instead of it.
    _ctx_limit = MODEL_CTX.get(current_model, int(os.getenv("OLLAMA_CONTEXT_LENGTH", "24576")))
    _budget_chars = int(_ctx_limit * 0.85 * 3) - len(task) - 2000  # 2000 ~= fixed system-prompt overhead
    _running = 0
    _kept: list[str] = []
    for _f in rel_files:
        _full = os.path.join(project_root, _f)
        _size = min(os.path.getsize(_full), 10000) if os.path.exists(_full) else 0
        if _kept and _running + _size > _budget_chars:
            continue  # always keep at least the first (target) file
        _running += _size
        _kept.append(_f)
    if len(_kept) < len(rel_files):
        print(f"  {DIM}(pre-trimmed {len(rel_files)} → {len(_kept)} files to fit "
              f"{current_model}'s {_ctx_limit}-token context){RESET}")
    rel_files = _kept

    print(f"  Files: {', '.join(rel_files) or '(none found)'}")

    # ── RAG retrieval (opt-in) ───────────────────────────────────────────────
    # find_relevant_files()/forced-inclusion above still choose WHICH files
    # are in scope — unchanged. RAG only changes how the non-target files in
    # that set get read into the prompt: chunk excerpts instead of whole
    # files. The task's own target file (_target_m) stays whole-file, since a
    # task needs full context of what it's creating/editing, not a fragment.
    rag_grouped: dict = {}
    if state.RAG_ENABLED:
        try:
            import rag_index, rag_retrieve
            rag_index.sync_index(project_root, candidate_files=rel_files)
            _target_path = _target_m.group(1) if _target_m else None
            # Never excerpt the always-included contract files (model.py,
            # API.md, API_ROWS.md…): an excerpt can drop exactly the fields a
            # model keeps inventing (2026-09-30). Only other support files are
            # chunked; these stay whole.
            support_files = [f for f in rel_files
                             if f != _target_path and f not in state.PROJECT_ALWAYS_INCLUDE]
            rag_grouped = rag_retrieve.retrieve_context_chunks(task, support_files, project_root)
            if rag_grouped:
                print(f"  {DIM}RAG: {sum(len(c) for c in rag_grouped.values())} chunk(s) "
                      f"across {len(rag_grouped)} of {len(support_files)} support file(s){RESET}")
        except Exception as exc:
            print(f"  {YELLOW}(RAG retrieval unavailable: {exc} — using whole-file context){RESET}")

    # Snapshot pre-existing errors so the validator only fails on NEW ones
    baseline_errors = snapshot_baseline_errors(project_root)

    backups = backup_files(rel_files, project_root)
    errors  = ""

    # ── Chronic blocker fast-path ─────────────────────────────────────────────
    # If this task has been skipped in previous runs, skip local models entirely
    # and call Claude directly with a richer, focused prompt.  If Claude also
    # fails, fall through to the normal tier loop as a last resort.
    if state.FINAL_TIER_ENABLED and CHRONIC_BLOCKER_THRESHOLD > 0:
        prior_skips = _count_prior_skips(task, project_root)
        if prior_skips >= CHRONIC_BLOCKER_THRESHOLD:
            file_contents_c = read_files(rel_files, project_root)
            claude_changes, claude_ok = _escalate_to_claude_chronic(
                task, file_contents_c, prior_skips, project_root, is_test)
            if claude_changes:
                state.REQUIRE_PATCH = set()  # escalation writes whole files on purpose
                written_c, pat_errs_c = write_changes(
                    claude_changes, project_root, test_only=is_test)
                if not pat_errs_c:
                    passed_c, output_c = validate(baseline_errors, files_written=written_c, task_gate=_extract_task_gate(task), regression_gates=regression_gates(task, files=written_c))
                    if passed_c:
                        print(f"  {GREEN}✓ Chronic blocker solved by Claude{RESET}")
                        update_api_guide(written_c, claude_changes, project_root)
                        _append_velocity(project_root, {
                            "date": date.today().isoformat(), "task": task[:120],
                            "outcome": "done", "attempts": 1,
                            "duration_s": round(time.time() - task_start, 1),
                            "error_types": [],
                            "claude_rescued": True,
                            "chronic_bypass": True,
                            "model": CLAUDE_MODEL,
                        })
                        _log_task_summary(project_root, task_idx, task, "done",
                                          1, CLAUDE_MODEL, task_start)
                        return True
                    print(
                        f"  {YELLOW}Claude chronic attempt failed — falling through "
                        f"to normal tiers{RESET}"
                    )
                    restore_files(backups, project_root)
            else:
                print(f"  {YELLOW}Claude returned no changes — trying normal tiers{RESET}")

    def _advance_tier(attempt: int) -> bool:
        """Move to the next tier whose model actually differs from the one
        that just got stuck. Returns False if no distinct-model tier remains.

        Historically this advanced to tier_idx+1's nominal model even when it
        was byte-identical to the model that just failed — every project
        running SINGLE-MODEL MODE (e.g. old-car-radio.env pins all four tiers
        to the same qwen3.8:27b-mlx) prints "escalating to tier3/tier4" while
        actually retrying the exact same weights. Observed directly: a stuck
        task burned its full attempt budget three more times against a model
        that had already failed identically, wasting ~40 minutes of wall
        clock before finally exhausting. Skip straight past any tier whose
        model matches the one that just got stuck; only a genuinely different
        model counts as an escalation.
        """
        nonlocal tier_idx, current_model, phase_stale, phase_attempts, last_err_sig, errors
        prev_model = TIER_MODELS[tier_idx]
        next_idx = tier_idx + 1
        while next_idx < _max_tier and TIER_MODELS[next_idx] == prev_model:
            next_idx += 1
        if next_idx >= _max_tier:
            return False
        tier_idx = next_idx
        next_model = TIER_MODELS[tier_idx]
        print(
            f"\n  {YELLOW}⚡ {prev_model} stuck after {PHASE_STRIKE_LIMIT} identical "
            f"errors — escalating to tier{tier_idx + 1} ({next_model}) "
            f"(attempt {attempt}){RESET}"
        )
        current_model  = next_model
        phase_stale    = 0
        phase_attempts = 0
        last_err_sig   = ""
        errors = (
            f"NOTE: A previous model ({prev_model}) failed {PHASE_STRIKE_LIMIT}× "
            f"with the same error. You are tier{tier_idx + 1}. "
            f"Read the error carefully and apply a DIFFERENT approach.\n\n" + errors
        )
        return True

    for attempt in range(1, MAX_RETRIES + 1):
        state.THINK_OVERRIDE = (OLLAMA_THINK_RETRIES
                                if attempt > 1 and OLLAMA_THINK_RETRIES is not None else None)
        heartbeat(f"task {task_idx} attempt {attempt}")
        # ── Absolute hard ceiling ────────────────────────────────────────────
        # Checked first, before any branch (timeout/bad-pattern/no-output/thrash)
        # gets a chance to `continue` past it. No bonuses, no tier-availability
        # exception — once a task blows past MAX_TASK_SECONDS it is done.
        hard_elapsed = time.time() - task_start
        if hard_elapsed > MAX_TASK_SECONDS:
            print(
                f"\n  {RED}⛔ Hard ceiling {MAX_TASK_SECONDS:.0f}s exceeded "
                f"({hard_elapsed:.0f}s elapsed) — skipping regardless of tier state{RESET}"
            )
            restore_files(backups, project_root)
            _append_velocity(project_root, {
                "date": date.today().isoformat(), "task": task[:120],
                "outcome": "budget", "attempts": attempt,
                "duration_s": round(hard_elapsed, 1),
                "error_types": list(dict.fromkeys(errors_seen + ["hard_ceiling"])),
            })
            _log_task_summary(project_root, task_idx, task, "budget",
                              attempt, current_model, task_start)
            return "skipped"

        # Snapshot error context at the top of each attempt so the trace record
        # captures exactly what failure context the model was given this round.
        errors_at_start = errors
        written: list[str] = []
        pattern_errs = ""
        race_stats: dict | None = None   # populated only during tier-0 race attempts
        tier_label = f" [tier{tier_idx + 1}]" if tier_idx > 0 else ""
        _racing = tier_idx == 0 and RACE_ENABLED
        _race_label = f" racing {TIER1_MODEL} vs {RACE_MODEL}" if _racing else f" ({current_model}){tier_label}"
        print(
            f"  {DIM}Attempt {phase_attempts + 1}/{_max_attempts_for_tier(tier_idx)} — coding"
            f"{_race_label}...{RESET}",
            end="", flush=True,
        )

        file_contents = read_files(rel_files, project_root)
        if rag_grouped:
            # read_files() itself is untouched; substitute chunk excerpts only
            # for files RAG found hits for. Any file RAG returns nothing for
            # (parse failure, no relevant chunks) keeps its whole-file content
            # from read_files() above — safe by construction.
            for _rag_path, _rag_chunks in rag_grouped.items():
                if _rag_path in file_contents:
                    file_contents[_rag_path] = rag_retrieve.format_chunks_for_prompt({_rag_path: _rag_chunks})

        # Exact-import-line hint (Dart): RAG already resolves the real,
        # on-disk path of every support file it hands the model — the model
        # still has to guess how deep '../' goes to reach it from whichever
        # file it's editing, and observed directly (2026-09-01/02,
        # old-car-radio) it guesses wrong constantly: 6+ distinct wrong
        # relative paths across 3+ tasks for files RAG had ALREADY retrieved
        # correct chunks of. A package: import needs no relative math at all
        # — it's the same string regardless of which file is doing the
        # importing — so hand the model the literal line instead of making
        # it derive one.
        #
        # test/ files have no package: form (package:<name>/... only maps
        # lib/), so a test importing a test/support/ helper hits the exact
        # same wrong-relative-path failure with no package: escape hatch —
        # observed directly (nope_button_test.dart guessing
        # '../../support/haptic_test_utils.dart', wrong, right after this
        # fix landed and still fixed the lib/ case fine). Compute the real
        # relative path instead: both files are known, so the math is exact.
        if state.PROJECT_LANGUAGE == "dart":
            _editing_file_m = re.search(r'\b(lib/[\w/]+\.dart|test/[\w/]+\.dart)\b', task)
            _editing_file = _editing_file_m.group(1) if _editing_file_m else None
            try:
                import dart_grounding as _dart_grounding
                _pkg_name, _ = _dart_grounding.pubspec_info(project_root)
            except Exception:
                _pkg_name = None
            for _fp in list(file_contents.keys()):
                if not _fp.endswith(".dart"):
                    continue
                _import_line = None
                _import_from = "anywhere else in the project"
                if _fp.startswith("lib/") and _pkg_name:
                    _import_line = f"import 'package:{_pkg_name}/{_fp[len('lib/'):]}';"
                elif (_fp.startswith("test/") and _editing_file
                      and _editing_file.startswith("test/") and _fp != _editing_file):
                    _rel = os.path.relpath(_fp, os.path.dirname(_editing_file))
                    _import_line = f"import '{_rel.replace(os.sep, '/')}';"
                    _import_from = _editing_file
                if _import_line:
                    file_contents[_fp] = (
                        f"// To import THIS file from {_import_from}, use exactly:\n"
                        f"// {_import_line}\n" + file_contents[_fp]
                    )

        if state.RAG_ENABLED and errors:
            try:
                import rag_failures
                _hist_fix = rag_failures.retrieve_similar_failure_fix(project_root, errors)
                if _hist_fix:
                    errors = errors + _hist_fix
            except Exception:
                pass

        try:
            if _racing:
                changes, call_info, race_winner, race_stats = _race_implement(
                    task, file_contents, errors, is_test, project_root)
                if race_winner:
                    current_model = race_winner  # track winner for logging
            else:
                changes, call_info = implement_task(task, file_contents, errors, model=current_model,
                                                    is_test=is_test, project_root=project_root)
        except (ReadTimeout, RequestsConnectionError) as exc:
            # A timed-out attempt produced no output, so it never reached
            # _log_attempt_trace — hour-long stalls left no data at all
            # (2026-09-29). Record what the model was given.
            _append_trace(project_root, {
                "record_type": "timeout", "trace_id": f"{task_idx}:{attempt}",
                "date": date.today().isoformat(), "task_idx": task_idx, "task": task,
                "attempt": attempt, "tier": tier_idx + 1, "model": current_model,
                "relevant_files": list(rel_files),
                "context_chars": sum(len(v) for v in file_contents.values()),
                "errors_fed_in_chars": len(errors or ""),
                "elapsed_s": round(time.time() - task_start, 1),
                "error": str(exc)[:300],
            })
            errors = f"Model timed out or connection failed: {exc}. Retry."
            errors_seen.append("timeout")
            print(f" {YELLOW}timeout — retrying{RESET}")
            phase_attempts += 1
            # TWO SILENT TIMEOUTS = OVER-SCOPED. A model that thinks for the
            # whole limit without writing code is not close; a third attempt
            # spends another 15 minutes the same way. compute_window and
            # collect each burned three (2026-09-30); split, each piece passed
            # in minutes. Escalate if a different model is left, else stop and
            # say the task needs splitting.
            _m = re.search(r"\((\d+) chars produced\)", str(exc))
            if _m and int(_m.group(1)) < 2000:
                silent_timeouts += 1
            if silent_timeouts >= 2:
                silent_timeouts = 0
                if not _advance_tier(attempt):
                    restore_files(backups, project_root)
                    _note_needs_split(project_root, task)
                    print(f"  {YELLOW}✂  two timeouts with no code — likely over-scoped; "
                          f"noted in logs/needs_split.md{RESET}")
                    _append_velocity(project_root, {
                        "date": date.today().isoformat(), "task": task[:120],
                        "outcome": "skipped", "attempts": attempt,
                        "duration_s": round(time.time() - task_start, 1),
                        "error_types": list(dict.fromkeys(errors_seen + ["silent_timeout"])),
                    })
                    _log_task_summary(project_root, task_idx, task, "timeout",
                                      attempt, current_model, task_start)
                    return "skipped"
                continue
            if phase_attempts >= _max_attempts_for_tier(tier_idx):
                if not _advance_tier(attempt):
                    restore_files(backups, project_root)
                    _append_velocity(project_root, {
                        "date": date.today().isoformat(), "task": task[:120],
                        "outcome": "skipped", "attempts": attempt,
                        "duration_s": round(time.time() - task_start, 1),
                        "error_types": list(dict.fromkeys(errors_seen)),
                    })
                    _log_task_summary(project_root, task_idx, task, "timeout",
                                      attempt, current_model, task_start)
                    return "skipped"
            continue
        except HTTPError as exc:
            # Ollama 500 = server-side OOM or model crash — wait briefly and retry
            print(f" {YELLOW}ollama 500 — retrying in 5s{RESET}")
            errors_seen.append("ollama_500")
            time.sleep(5)
            phase_attempts += 1
            if phase_attempts >= _max_attempts_for_tier(tier_idx):
                if not _advance_tier(attempt):
                    restore_files(backups, project_root)
                    _append_velocity(project_root, {
                        "date": date.today().isoformat(), "task": task[:120],
                        "outcome": "skipped", "attempts": attempt,
                        "duration_s": round(time.time() - task_start, 1),
                        "error_types": list(dict.fromkeys(errors_seen)),
                    })
                    _log_task_summary(project_root, task_idx, task, "timeout",
                                      attempt, current_model, task_start)
                    return "skipped"
            continue

        except PromptTooLargeError as exc:
            errors_seen.append("prompt_too_large")
            if not context_trimmed:
                # First hit: cut context files in half and retry at same tier.
                # This is usually enough — the model was loading too many large files.
                keep = max(3, len(rel_files) // 2)
                rel_files = rel_files[:keep]
                context_trimmed = True
                print(
                    f" {YELLOW}prompt too large ({exc.token_count}/{exc.limit} tokens) "
                    f"— trimming to {keep} files and retrying{RESET}"
                )
            else:
                # Already trimmed and still overflowing — this task needs a bigger
                # context window.  Queue it for the --deep pass (which can be run
                # with OLLAMA_CONTEXT_LENGTH=16384 or fewer parallel workers).
                print(
                    f" {YELLOW}prompt still too large after trim "
                    f"({exc.token_count}/{exc.limit} tokens) — queuing for --deep run{RESET}"
                )
                _write_tier2_queue(project_root, task_idx, task, ["prompt_too_large"])
                restore_files(backups, project_root)
                _append_velocity(project_root, {
                    "date": date.today().isoformat(), "task": task[:120],
                    "outcome": "skipped", "attempts": attempt,
                    "duration_s": round(time.time() - task_start, 1),
                    "error_types": list(dict.fromkeys(errors_seen)),
                })
                return "skipped"
            continue

        if not changes:
            errors_seen.append("no_output")
            print(f" {YELLOW}no output{RESET}")
            # 2026-07-10: this branch used to `continue` without ever logging a
            # trace record — "no output" failures were completely invisible in
            # task_traces.jsonl (no raw model response captured anywhere), which
            # made root-causing them impossible after the fact. Log it now, same
            # as every other attempt outcome, so the raw text survives for
            # inspection (empty response vs malformed JSON vs truncated prompt
            # all look identical from the console alone).
            _log_attempt_trace(project_root, task_idx, task, attempt,
                               tier_idx + 1, current_model, is_test, rel_files,
                               call_info, [], False,
                               False, "(no output — see raw_output)", errors_at_start,
                               False, task_start)
            phase_stale += 1
            if phase_stale >= PHASE_STRIKE_LIMIT:
                if not _advance_tier(attempt):
                    # All tiers exhausted on empty output
                    restore_files(backups, project_root)
                    _append_velocity(project_root, {
                        "date": date.today().isoformat(), "task": task[:120],
                        "outcome": "skipped", "attempts": attempt,
                        "duration_s": round(time.time() - task_start, 1),
                        "error_types": list(dict.fromkeys(errors_seen)),
                    })
                    _log_task_summary(project_root, task_idx, task, "no_output",
                                      attempt, current_model, task_start)
                    return "skipped"
            continue

        last_skipped = [p for p in changes if p in LOCKED_FILES]
        # Go tasks name their single target file: "In <path>.go: implement ..."
        _go_target = None
        if state.PROJECT_LANGUAGE == "go":
            _m = re.search(r'\bIn `?([\w./-]+\.go)`?:', task)
            _go_target = _m.group(1) if _m else None
        written, pattern_errs = write_changes(changes, project_root, test_only=is_test,
                                              required_file=_go_target)
        last_written = written

        # Functions the task did not ask to change go back as they were
        # (sov/preserve.py) — models regenerate whole files from memory.
        if state.PROJECT_LANGUAGE == "python" and not is_test:
            from sov.preserve import restore_files as _restore_untouched
            _put_back = _restore_untouched(task, list(written), project_root)
            if _put_back:
                print(f"  {DIM}(kept unchanged: {', '.join(_put_back)}){RESET}")

        _unplanned = _unplanned_new_files(written, task, project_root)
        if _unplanned:
            pattern_errs = list(pattern_errs) + [
                "INVENTED FILE [" + ", ".join(_unplanned) + "]: this task did not "
                "ask you to create that file, and it does not exist in the project. "
                "You are almost certainly writing a placeholder for something that "
                "ALREADY EXISTS elsewhere — find the real definition and import it. "
                "Write ONLY the file the task names. Never create a stub, an empty "
                "function body, or a `=> []` / `return null` placeholder to make the "
                "build pass; a stub compiles and is still wrong."
            ]
            for _p in _unplanned:
                try:
                    os.remove(os.path.join(project_root, _p))
                except OSError:
                    pass
            print(f"  {RED}(blocked invented file: {', '.join(_unplanned)}){RESET}")

        _nosym = _missing_promised_symbol(written, task, project_root) or \
            _placeholder_body(written, task, project_root)
        if _nosym:
            pattern_errs = list(pattern_errs) + [_nosym]
            print(f"  {RED}(blocked: task not actually implemented){RESET}")

        # Used the contract, or copied it? (sov/contract_use.py)
        if state.PROJECT_LANGUAGE == "python" and not _nosym:
            _copied = unused_contract(task, list(written), project_root)
            if _copied:
                pattern_errs = list(pattern_errs) + [_copied]
                print(f"  {RED}(blocked: copied instead of calling the existing function){RESET}")

        if pattern_errs:
            errors = "\n".join(pattern_errs) + ("\n\n" + errors if errors else "")
            errors_seen.append("bad_pattern")
            _ledger(project_root, task_idx, current_model,
                    "attempt_blocked", pattern_errs)
            consecutive_bad_pattern += 1
            print(f" {YELLOW}blocked by bad patterns — retrying{RESET}")
            # After 3 consecutive bad-pattern blocks the model is stuck in a loop.
            # Escalate to the next tier rather than spinning indefinitely.
            if consecutive_bad_pattern >= 3:
                print(
                    f"  {YELLOW}⚡ bad-pattern loop ({consecutive_bad_pattern}×) — "
                    f"escalating tier{RESET}"
                )
                consecutive_bad_pattern = 0
                if not _advance_tier(attempt):
                    restore_files(backups, project_root)
                    _write_tier2_queue(project_root, task_idx, task, errors_seen)
                    _append_velocity(project_root, {
                        "date": date.today().isoformat(), "task": task[:120],
                        "outcome": "skipped", "attempts": attempt,
                        "duration_s": round(time.time() - task_start, 1),
                        "error_types": list(dict.fromkeys(errors_seen)),
                    })
                    _log_task_summary(project_root, task_idx, task, "skipped",
                                      attempt, current_model, task_start)
                    return "skipped"
            continue

        heartbeat(f"task {task_idx} wrote files")
        consecutive_bad_pattern = 0   # model wrote real code — reset bad-pattern streak
        changed_list = ", ".join(written)
        print(f" wrote {len(written)} file(s): {changed_list[:60]}")

        # Pre-format Go files BEFORE the first validation (2026-07-13).
        # Local models emit space-indented Go, so validate #1 always died at
        # the gofmt gate, and the post-autofix failure produced a different
        # error signature each attempt — feeding the thrash detector bogus
        # "all different errors" streaks and escalating tiers prematurely.
        if state.PROJECT_LANGUAGE == "go":
            try:
                _get_autofix().apply_go_mechanical_fixes(project_root, written)
            except Exception:
                pass

        # Mechanically repair TS imports BEFORE the first validation: relocate
        # wrong-module symbols to their real module and drop invented ones. This
        # deterministically kills the TS2305/TS2307 "invented import" loop — the
        # dominant local-model failure — without the model ever getting it right.
        if any(w.endswith((".ts", ".tsx")) for w in written):
            try:
                idx, exp, _sigs = _symbol_index(project_root, refresh=True)
                _fixes = import_fixer.fix_ts_imports(project_root, written, idx, exp)
                if _fixes:
                    errors_seen.append("import_autofixed")
                    print(f" {DIM}[import-fix: {len(_fixes)}]{RESET}", end="", flush=True)
                    with open(log_file, "a") as _lg:
                        _lg.write("Import autofix:\n  " + "\n  ".join(_fixes) + "\n")
            except Exception as _e:
                print(f"  {DIM}(import-fix error: {_e}){RESET}")

        # Mechanically repair Dart imports BEFORE validation. The dominant
        # local-model failure in Dart is correct code referencing a real project
        # symbol the model could not locate. Adding the import costs zero tokens;
        # re-prompting to regenerate the file to add one line costs an attempt.
        if any(x.endswith(".dart") for x in written):
            try:
                _fixes = _get_dart_import_fixer().fix_dart_imports(project_root, written)
                for _f in _fixes:
                    print(f"  {DIM}import fix: {_f}{RESET}")
            except Exception:
                pass

        print(f"  {DIM}Validating...{RESET}", end="", flush=True)
        passed, output = validate(baseline_errors, files_written=written, task_gate=_extract_task_gate(task), regression_gates=regression_gates(task, files=written))

        with open(log_file, "a") as log:
            log.write(f"\n### Attempt {attempt}\nFiles changed: {changed_list}\n")
            log.write(f"Validation: {'PASSED' if passed else 'FAILED'}\n{output}\n")

        if not passed:
            _ledger(project_root, task_idx, current_model,
                    "validate_failed", output)

        if race_stats:
            _append_race(project_root, {
                "date": date.today().isoformat(), "task": task[:80],
                "attempt": attempt, "validation_passed": passed,
                **race_stats,
            })

        if passed:
            print(f" {GREEN}✓ passed{RESET}")
            update_api_guide(written, changes, project_root)
            _append_velocity(project_root, {
                "date": date.today().isoformat(), "task": task[:120],
                "outcome": "done", "attempts": attempt,
                "duration_s": round(time.time() - task_start, 1),
                "error_types": list(dict.fromkeys(errors_seen)),
                "model": current_model,
            })
            _log_attempt_trace(project_root, task_idx, task, attempt,
                               tier_idx + 1, current_model, is_test, rel_files,
                               call_info, written, bool(pattern_errs),
                               True, output, errors_at_start, False, task_start)
            _log_task_summary(project_root, task_idx, task, "done",
                              attempt, current_model, task_start)
            return True

        # ── Failed validation ─────────────────────────────────────────────
        first_error = next((l for l in output.splitlines() if "error" in l.lower()), output[:120])
        print(f" {RED}✗ {first_error[:100]}{RESET}")
        errors_seen.extend(_extract_error_codes(output))

        # Whose failure is this, actually? A gate can fail because of a file a
        # PREVIOUS task merged — the classic is a worker that redeclared a locked
        # type or guessed a locked constant. That lands green on an analyze-only
        # gate and detonates in the first conformance gate that imports it. The
        # harness then blames the task that happens to be running, escalates ITS
        # model, and burns ITS budget on a file it never wrote and cannot fix.
        try:
            _foreign, _mine = blame_split(output, written, _gate_tests)
            if _foreign and not _mine:
                foreign_streak += 1
                print(f"  {YELLOW}⚠  No error names a file this task wrote. "
                      f"Blamed files: {', '.join(_foreign[:3])}{RESET}")
                print(f"  {DIM}   The cause is probably an earlier merge or an "
                      f"unsatisfiable gate, not this task. Escalating the model "
                      f"cannot fix it.{RESET}")
                errors_seen.append("foreign_failure")

                # Cross-file dependency gate (2026-09-08): the diagnosis above
                # already establishes escalating THIS task's model cannot fix
                # it — the blamed file belongs to a different task. Previously
                # execution fell through to the normal retry/escalate ladder
                # anyway, burning a second attempt (often across a tier
                # change) on a failure already known to be unfixable here.
                # Observed cost: a required-param addition in file A whose
                # only call site is in file B (a separate, later task) failed
                # validation on attempt 1 with this exact diagnosis, then
                # spent attempt 2 re-trying at tier2 before exhausting its
                # budget and queuing for --deep — a fully avoidable second
                # attempt.
                #
                # Bounded by prior-skip count (persisted across runs, not
                # just this call) so this can't quietly disable the
                # foreign_streak/FOREIGN_PARK_LIMIT safety net below: a task
                # that keeps landing here across repeated separate attempts
                # is NOT a simple ordering issue (the true dependency isn't
                # landing, or this blame is a coincidence) and needs the
                # normal retry-then-park path to eventually flag it for
                # review, not an indefinite fast-defer loop.
                if _count_prior_skips(task, project_root) < 2:
                    print(f"  {DIM}   Deferring to retry pass now instead of "
                          f"burning further attempts on an unfixable failure.{RESET}")
                    restore_files(backups, project_root)
                    _append_velocity(project_root, {
                        "date": date.today().isoformat(), "task": task[:120],
                        "outcome": "skipped", "attempts": attempt,
                        "duration_s": round(time.time() - task_start, 1),
                        "error_types": list(dict.fromkeys(errors_seen)),
                        "model": current_model,
                    })
                    _log_task_summary(project_root, task_idx, task, "skipped",
                                      attempt, current_model, task_start)
                    return "skipped"
        except Exception:
            pass

        # Park instead of climbing the ladder. Non-blocking: it records the
        # diagnosis, skips the task, and lets the run carry on, so an overnight
        # session is not held hostage by one bad merge.
        if foreign_streak >= FOREIGN_PARK_LIMIT:
            elapsed_park = time.time() - task_start
            print(f"\n  {RED}⏸  Parked for review — {foreign_streak} failures "
                  f"naming no file this task wrote{RESET}")
            print(f"  {DIM}   Wrote {REVIEW_LOG}. Not escalating; a larger model "
                  f"cannot reach this.{RESET}")
            try:
                _park_for_review(project_root, task_idx, task, current_model,
                                 written, _foreign, output)
            except Exception:
                pass
            restore_files(backups, project_root)
            _append_velocity(project_root, {
                "date": date.today().isoformat(), "task": task[:120],
                "outcome": "needs_review", "attempts": attempt,
                "duration_s": round(elapsed_park, 1),
                "error_types": list(dict.fromkeys(errors_seen)),
                "model": current_model,
            })
            _log_task_summary(project_root, task_idx, task, "needs_review",
                              attempt, current_model, task_start)
            return "skipped"

        # Step 1: Mechanical auto-fixer
        try:
            af = _get_autofix()
            if state.PROJECT_LANGUAGE == "go":
                fix_count, _ = af.apply_go_mechanical_fixes(project_root, written)
            else:
                fix_count, _ = af.apply_mechanical_fixes(output, project_root)
            if fix_count > 0:
                print(f"  {DIM}⚙  Auto-fixed {fix_count} issue(s) — re-validating...{RESET}", end="", flush=True)
                passed2, output2 = validate(baseline_errors, files_written=written, task_gate=_extract_task_gate(task), regression_gates=regression_gates(task, files=written))
                if passed2:
                    print(f" {GREEN}✓ passed after autofix{RESET}")
                    errors_seen.append("autofix_resolved")
                    update_api_guide(written, changes, project_root)
                    _append_velocity(project_root, {
                        "date": date.today().isoformat(), "task": task[:120],
                        "outcome": "done", "attempts": attempt,
                        "duration_s": round(time.time() - task_start, 1),
                        "error_types": list(dict.fromkeys(errors_seen)),
                        "model": current_model,
                    })
                    _log_attempt_trace(project_root, task_idx, task, attempt,
                                       tier_idx + 1, current_model, is_test, rel_files,
                                       call_info, written, bool(pattern_errs),
                                       True, output2, errors_at_start, True, task_start)
                    _log_task_summary(project_root, task_idx, task, "done",
                                      attempt, current_model, task_start)
                    return True
                print(f" {YELLOW}still failing{RESET}")
                output = output2
        except Exception as e:
            print(f"  {DIM}(autofix error: {e}){RESET}")

        # Step 2: qwen advisor — tier 1 only (2026-07-10). The advisor runs on
        # ADVISOR_MODEL, which is the same 7B weights as TIER1_MODEL, so a
        # tier-1 call costs nothing extra (already resident in VRAM). But for
        # tier 2-4 (gemma4:26b / qwen3.6:35b / qwen2.5-coder:32b) it evicts
        # that much larger model from GPU every retry and forces a full
        # reload afterward — confirmed ~20-30s per retry. Not worth an
        # enriched hint on the rare tier 2-4 attempt. --no-advisor still
        # works as an explicit override to disable it for tier 1 too.
        advisor_hint = ""
        if not skip_advisor and tier_idx == 0:
            try:
                adv = _get_advisor()
                adv.log_error_pattern(output, task, attempt, task_idx, project_root, categories=[])
                print(f"  {DIM}🤖 Asking qwen advisor...{RESET}", end="", flush=True)
                advice = adv.advise(output, task, project_root, attempt=attempt)
                print(f" {DIM}done{RESET}")
                if advice.get("new_rule"):
                    adv.log_rule_draft(advice["new_rule"], task_idx, project_root)
                    print(f"  {DIM}📝 Rule draft logged{RESET}")
                advisor_hint = advice.get("enriched_hint", "")
                # The hint is prepended to the next attempt's error text, so a
                # hallucinated identifier in it primes the model to write code
                # that cannot compile — the same failure mode as the 2026-07-14
                # poisoned .roorules entry, just with a one-attempt blast radius
                # instead of a permanent one. Same gate applies.
                if advisor_hint:
                    _v = prompt_artifacts.verify_prompt_artifact(
                        advisor_hint, project_root, kind="advisor hint",
                        mode="reject", check_code_blocks=True,
                    )
                    if not _v.ok:
                        print(f"  {YELLOW}✗ Advisor hint dropped — "
                              f"{_v.summary()}{RESET}")
                        _ledger(project_root, task_idx, current_model,
                                "advisor_hint_rejected", _v.summary())
                        advisor_hint = ""
                cats = advice.get("categories", [])
                errors_seen.append(f"qwen:{','.join(str(c) for c in (cats if isinstance(cats, list) else []))}")
            except Exception as e:
                print(f"  {DIM}(advisor error: {e}){RESET}")

        # ── Log this attempt (validation failed) ─────────────────────────
        # Captured here so errors_fed_in reflects what the model saw this round
        # and advisor_hint (now in `errors`) will appear as errors_fed_in next round.
        _log_attempt_trace(project_root, task_idx, task, attempt,
                           tier_idx + 1, current_model, is_test, rel_files,
                           call_info, written, bool(pattern_errs),
                           False, output, errors_at_start, False, task_start)

        # ── Time budget check ─────────────────────────────────────────────
        elapsed = time.time() - task_start
        effective_budget = (budget_s if budget_s is not None
                            else compute_task_budget(project_root))
        if elapsed > effective_budget:
            if _advance_tier(attempt):
                print(
                    f"\n  {YELLOW}⏱  Time budget {effective_budget:.0f}s exceeded "
                    f"({elapsed:.0f}s elapsed) — escalating with 300s bonus{RESET}"
                )
                # Give the next tier a fresh 5-minute window
                budget_s = (budget_s or effective_budget) + 300
                continue

            print(
                f"\n  {YELLOW}⏱  Time budget {effective_budget:.0f}s exceeded "
                f"({elapsed:.0f}s elapsed) — all tiers exhausted, skipping{RESET}"
            )
            restore_files(backups, project_root)
            _append_velocity(project_root, {
                "date": date.today().isoformat(), "task": task[:120],
                "outcome": "budget", "attempts": attempt,
                "duration_s": round(elapsed, 1),
                "error_types": list(dict.fromkeys(errors_seen)),
            })
            _log_task_summary(project_root, task_idx, task, "budget",
                              attempt, current_model, task_start)
            return "skipped"

        # Step 3: Stale-error detection + thrash cap — track streak per tier
        phase_attempts += 1
        cur_sig = _hl.md5(
            " ".join(sorted(_extract_error_codes(output))).encode()
        ).hexdigest()[:8]
        if cur_sig == last_err_sig:
            phase_stale += 1
        else:
            phase_stale = 0
        last_err_sig = cur_sig

        # Build enriched context for next attempt
        errors = apply_error_hints(output)
        # Deterministic import correction from the symbol index — precise, unlike
        # the LLM advisor which tends to re-guess the same wrong module.
        try:
            _idx, _exp, _sigs = _symbol_index(project_root)
            _imp_hint = import_fixer.import_error_hint(output, project_root, _idx, _exp)
            _sig_hint = import_fixer.signature_error_hint(output, project_root, _sigs)
            _combined = "\n\n".join(h for h in (_sig_hint, _imp_hint) if h)
            if _combined:
                errors = f"{_combined}\n\n{errors}"
        except Exception:
            pass
        if advisor_hint:
            errors = f"⚠️  Advisor note: {advisor_hint}\n\n{errors}"

        thrashing = phase_attempts >= _max_attempts_for_tier(tier_idx)
        if thrashing and phase_stale < PHASE_STRIKE_LIMIT:
            print(
                f"\n  {YELLOW}⚡ {current_model} thrashing — {phase_attempts} attempts, "
                f"all different errors — escalating to next tier (attempt {attempt}){RESET}"
            )
        if phase_stale >= PHASE_STRIKE_LIMIT or thrashing:
            if not _advance_tier(attempt):
                # All local tiers exhausted — try Claude before giving up
                reason = (
                    f"thrashing ({phase_attempts} attempts, all-different errors)"
                    if thrashing and phase_stale < PHASE_STRIKE_LIMIT
                    else f"same error {PHASE_STRIKE_LIMIT}× each"
                )
                print(f"\n  {YELLOW}⏭  All local tiers exhausted ({reason}){RESET}")

                if state.FINAL_TIER_ENABLED:
                    restore_files(backups, project_root)  # clean slate for the final tier
                    file_contents_claude = read_files(rel_files, project_root)
                    claude_changes, _ = _escalate_to_claude(
                        task, file_contents_claude, errors, project_root, is_test)
                    if claude_changes:
                        state.REQUIRE_PATCH = set()  # escalation writes whole files on purpose
                        written_c, pat_errs_c = write_changes(
                            claude_changes, project_root, test_only=is_test)
                        if not pat_errs_c:
                            passed_c, output_c = validate(baseline_errors, files_written=written_c, task_gate=_extract_task_gate(task), regression_gates=regression_gates(task, files=written_c))
                            if passed_c:
                                print(f"  {GREEN}✓ Claude solved it{RESET}")
                                update_api_guide(written_c, claude_changes, project_root)
                                _append_velocity(project_root, {
                                    "date": date.today().isoformat(), "task": task[:120],
                                    "outcome": "done", "attempts": attempt + 1,
                                    "duration_s": round(time.time() - task_start, 1),
                                    "error_types": list(dict.fromkeys(errors_seen)),
                                    "claude_rescued": True,
                                    "model": CLAUDE_MODEL,
                                })
                                _log_task_summary(project_root, task_idx, task, "done",
                                                  attempt + 1, CLAUDE_MODEL, task_start)
                                return True
                            print(f"  {YELLOW}Claude attempt failed validation — skipping{RESET}")
                            restore_files(backups, project_root)

                _append_velocity(project_root, {
                    "date": date.today().isoformat(), "task": task[:120],
                    "outcome": "skipped", "attempts": attempt,
                    "duration_s": round(time.time() - task_start, 1),
                    "error_types": list(dict.fromkeys(errors_seen)),
                })
                _log_task_summary(project_root, task_idx, task, "skipped",
                                  attempt, current_model, task_start)
                _write_escalation(project_root, task_idx, task, attempt, errors,
                                  last_written, last_skipped,
                                  is_test=is_test, identical_error=phase_stale >= PHASE_STRIKE_LIMIT)
                return "skipped"
        elif phase_stale > 0:
            # Still in the same tier but looping — add a hard-override warning
            errors = (
                f"🚨 SAME ERROR {phase_stale + 1}× IN A ROW: Your approach is wrong. "
                f"Try a completely different fix. If 'undefined_class', add the import — "
                f"do NOT define a new class. If wrong method name, check .roorules.\n\n"
                + errors
            )

    # Exhausted MAX_RETRIES without a stale lock — restore and report
    print(f"  {RED}✗ {MAX_RETRIES} attempts failed — restoring original files{RESET}")
    restore_files(backups, project_root)
    _append_velocity(project_root, {
        "date": date.today().isoformat(), "task": task[:120],
        "outcome": "failed", "attempts": MAX_RETRIES,
        "duration_s": round(time.time() - task_start, 1),
        "error_types": list(dict.fromkeys(errors_seen)),
    })
    _log_task_summary(project_root, task_idx, task, "failed",
                      MAX_RETRIES, current_model, task_start)
    return False
