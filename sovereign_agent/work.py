"""
work.py — Fully autonomous task execution loop.

After standup.py approves today's tasks, this script runs unattended:
  1. Reads every unchecked task for today from ROADMAP.md
  2. Uses Ollama 4B to identify which files are relevant
  3. Uses Ollama 35B to implement the changes (returns complete file contents)
  4. Runs flutter analyze / pytest to validate
  5. On failure:
     a. autofix.py applies deterministic mechanical fixes (zero API cost)
     b. qwen_advisor.py (ADVISOR_MODEL — same 7B weights as Tier 1, Tier 1
        attempts only; skipped on Tier 2-4 to avoid evicting the larger
        model from GPU) classifies remaining errors, enriches context,
        and drafts new .roorules entries
     c. the current tier's model retries with enriched error context
  6. Marks each task [x] in ROADMAP.md when done
  7. Moves to the next task — no human prompts, ever

Usage:
    python work.py --project ~/Code/astro_flux
    python work.py --project ~/Code/astro_flux --start-at 3
    python work.py --project ~/Code/astro_flux --dry-run
"""

from datetime import date
from dotenv import load_dotenv
import argparse
import json
import os
import re
import subprocess
import sys
import time

# .env must load before any sov module reads the environment (sov.config
# resolves models, tiers and budgets at import time).
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

import grounders  # noqa: E402
from sov.config import (  # noqa: E402
    ADVISOR_MODEL,
    BUDGET_CEILING_S,
    BUDGET_MULTIPLIER,
    GIT_BRANCHES,
    MAIN_BRANCH,
    MODEL_PARAMS,
    PHASE_MAX_ATTEMPTS,
    PLANNER_MODEL,
    QUICK_PARAM_LIMIT_B,
    RACE_ENABLED,
    RACE_MODEL,
    RETRY_BUDGET_MULT,
    TIER2_MAX_ATTEMPTS,
    TIER4_MAX_ATTEMPTS,
    TIER_MODELS,
    ctx_conflicts as _ctx_conflicts,
)
from sov import state
from sov.budget import compute_task_budget, print_timing_report
from sov.escalation import TIER2_QUEUE_FILE, _write_incomplete_report, _write_tier2_queue
from sov.gitops import _branch_abandon, _branch_merge, _branch_start, _git, _post_planning_commit, _setup_worker_worktree
from sov.lessons import FIRESTORE_PROJECT, _firestore_pull_lessons, _firestore_push_lessons
from sov.llm import preflight_models
from sov.project import _load_project_config
from sov.roadmap import _is_test_task, mark_done, parse_all_tasks, task_text
from sov.runner import QUICK_MAX_TIER_IDX, run_task
from sov.scheduling import _feature_stem, _task_primary_file, tasks_for_worker
from sov.telemetry import _ledger
from sov import routing
from sov.ui import BOLD, DIM, GREEN, RED, RESET, YELLOW
from sov.watchdog import _start_stall_watchdog

_cw = _ctx_conflicts()
if _cw:
    print(_cw)
LOG_DIR        = "logs"
EXIT_NOTHING_ATTEMPTABLE = 5   # supervisor.sh ends the pass on this


# ─── Main ─────────────────────────────────────────────────────────────────────
# main() is the run's outline; each step below is a function whose body moved
# unchanged out of the former 700-line main() (2026-09-28).

def main():
    _start_stall_watchdog()
    args = _build_parser().parse_args()
    project_root = _resolve_project_root(args)

    counting = bool(args.remaining_count or args.queue_remaining_count)
    if not counting:
        _pre_run_guards(project_root)
        _load_config_and_preflight(project_root)
    _answer_count_query(args, project_root)  # exits if a count was asked for

    # ── Sprint planning commit (--commit-sprint) ──────────────────────────────
    # Must run before the task loop so cloud workers can pull ROADMAP.md + DAG.
    if args.commit_sprint:
        print(f"\n{BOLD}  📦  Committing sprint plan to git...{RESET}")
        _post_planning_commit(project_root)

    # ── Firestore pull: merge remote lessons before we start ──────────────────
    # Gives this worker benefit of everything other workers have already learned.
    if FIRESTORE_PROJECT:
        _firestore_pull_lessons(project_root)

    _ensure_flutter_deps(project_root, args.worker_id)

    # ── Effective budget for this run ─────────────────────────────────────────
    # Computed lazily after deep-mode resolution (which may set budget_multiplier).
    # In parallel mode (stride > 1) workers compete for the same Ollama slot,
    # so supervisor.sh passes --budget-multiplier equal to the worker count.
    # In deep mode we double the multiplier automatically (heavier models, longer tasks).
    first_pass_budget: float | None = None   # computed lazily on first task

    def get_first_pass_budget() -> float:
        nonlocal first_pass_budget
        if first_pass_budget is None:
            _bm = args.budget_multiplier if args.budget_multiplier is not None else BUDGET_MULTIPLIER
            first_pass_budget = compute_task_budget(
                project_root, multiplier=_bm, ceiling_s=BUDGET_CEILING_S
            )
        return first_pass_budget

    worker_suffix = f"-w{args.worker_id}" if args.stride > 1 else ""
    project_root = _setup_parallel_worker(args, project_root, worker_suffix)

    today_str = date.today().isoformat()

    run_start_tier_idx, run_max_tier_idx = _resolve_tier_range(args)
    tasks, run_start_tier_idx = _select_tasks(args, project_root,
                                              run_start_tier_idx, run_max_tier_idx)

    indexed_tasks = tasks_for_worker(tasks, args.stride, args.worker_id)
    run_models = TIER_MODELS[run_start_tier_idx:(run_max_tier_idx or len(TIER_MODELS))]
    if routing.is_strong_run(project_root, run_models, MODEL_PARAMS):
        # Fewest strong-model failures first, so untried tasks aren't starved
        # behind the same hard failures every cycle (sov/routing.py).
        indexed_tasks = routing.order_for_strong(indexed_tasks, routing.load_strikes(project_root),
                                                 task_text)
    if not indexed_tasks:
        if tasks and args.stride > 1:
            # Tasks remain, just none assigned to this worker: idle, not done —
            # otherwise the supervisor never sees "every worker idle" and
            # respawns once a second (found testing exit 5, 2026-09-30).
            print(f"  {DIM}worker {args.worker_id}: no tasks assigned this pass.{RESET}")
            sys.exit(EXIT_NOTHING_ATTEMPTABLE)
        print(f"{YELLOW}No unchecked tasks found anywhere in ROADMAP.md.{RESET}")
        print(f"Add tasks to ROADMAP.md or run plan_week.py first.")
        sys.exit(0)

    # Disable Claude escalation in --quick and --deep runs to save API costs
    # and keep these passes purely local-model based.
    if args.quick or args.deep:
        state.CLAUDE_ENABLED = False
        state.FINAL_TIER_ENABLED = False

    _print_banner(args, today_str, indexed_tasks, run_start_tier_idx, run_max_tier_idx)
    if not args.dry_run:
        _verify_models(run_start_tier_idx, run_max_tier_idx)

    os.makedirs(LOG_DIR, exist_ok=True)

    log_file = os.path.join(LOG_DIR, f"{today_str}-work{worker_suffix}.log")
    with open(log_file, "w") as f:
        f.write(f"# Work log — {today_str}\nProject: {project_root}\n"
                + (f"Worker: {args.worker_id}/{args.stride}\n" if args.stride > 1 else ""))

    session_start = time.time()
    passed_count, failed_count, skipped_tasks, retry_queue, attempted = _first_pass(
        args, indexed_tasks, project_root, log_file, get_first_pass_budget,
        run_start_tier_idx, run_max_tier_idx)
    skipped_count = len(skipped_tasks)

    # ── End-of-run summary ────────────────────────────────────────────────────
    print(f"\n{BOLD}{'━'*56}{RESET}")
    print(f"  {GREEN}✓ Done: {passed_count}{RESET}  "
          f"{YELLOW}⏭ Skipped: {skipped_count}{RESET}  "
          f"{RED}✗ Failed: {failed_count}{RESET}")
    print(f"  Log: {log_file}")
    print(f"{BOLD}{'━'*56}{RESET}\n")

    print_timing_report(project_root, session_start)

    if skipped_tasks:
        _write_incomplete_report(project_root, skipped_tasks, log_file)

    if retry_queue and not args.dry_run and not args.no_retry:
        _retry_pass(args, retry_queue, project_root, log_file, session_start)
        attempted += len(retry_queue)

    # ── Deep run cleanup ──────────────────────────────────────────────────────
    # After a --deep run completes (including its retry pass), clear the queue
    # file so the next --quick run starts fresh.  Tasks still failing after
    # the deep run are already noted in the incomplete report / velocity log.
    if args.deep:
        queue_file = os.path.join(project_root, TIER2_QUEUE_FILE)
        if os.path.exists(queue_file):
            open(queue_file, "w").close()
            print(f"  {DIM}tier2 queue cleared — run --quick to re-populate{RESET}\n")

    # ── Firestore push: share everything we learned this session ─────────────
    # Uploads error patterns, rule drafts, and velocity so cloud workers and
    # future local runs start with the full accumulated knowledge base.
    if FIRESTORE_PROJECT:
        _firestore_push_lessons(project_root)

    # Nothing attemptable: every task was deferred by routing or blocked on a
    # dependency. Exit 5 so supervisor.sh ends the pass instead of respawning
    # workers until its time cap (2026-09-29: 65 min, 0 attempts, 38 respawns).
    if attempted == 0 and indexed_tasks and not args.dry_run:
        print(f"  {DIM}No task was attemptable in this run (all deferred or blocked).{RESET}")
        sys.exit(EXIT_NOTHING_ATTEMPTABLE)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project",   help="Path to project folder", default=None)
    parser.add_argument("--start-at",  type=int, default=1,
                        help="Skip to task N (1-indexed, for resuming)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print tasks without executing")
    parser.add_argument("--no-retry",  action="store_true",
                        help="Skip the automatic retry pass over failed/skipped tasks")
    parser.add_argument("--features-only", action="store_true",
                        help="Skip tasks identified as tests")
    parser.add_argument("--tests-only",    action="store_true",
                        help="Skip tasks NOT identified as tests")
    parser.add_argument("--worker-id", type=int,
                        default=int(os.getenv("WORKER_ID", "0")),
                        help="Worker index for parallel execution (0-based); also reads WORKER_ID env var")
    parser.add_argument("--stride",    type=int,
                        default=int(os.getenv("STRIDE", "1")),
                        help="Total number of parallel workers (1 = sequential); also reads STRIDE env var")
    parser.add_argument("--budget-multiplier", type=float, default=None,
                        help="Override BUDGET_MULTIPLIER for this run "
                             "(e.g. pass --stride value so parallel workers "
                             "get proportionally more time to account for "
                             "Ollama queue depth)")
    parser.add_argument("--start-tier", type=int, default=None,
                        help="Start at tier N (1-indexed, skip T1..T(N-1)). "
                             "Use for complex tasks that benefit from larger models. "
                             "E.g. --start-tier 4 starts at qwen2.5-coder:32b.")
    parser.add_argument("--max-tier", type=int, default=None,
                        help="Cap the tier ladder at N (1-indexed). "
                             "Tasks that fail at tier N are written to "
                             "logs/tier2_queue.jsonl for a --deep run instead "
                             "of escalating. --quick sets this to 1.")
    parser.add_argument("--quick", action="store_true",
                        help="Fast sweep using tiers 1-3 (MoE models only). "
                             "Failures are queued in logs/tier2_queue.jsonl "
                             "for a follow-up --deep run (tier 4 + Claude).")
    parser.add_argument("--deep", action="store_true",
                        help="Process only tasks queued by a previous --quick "
                             "run (reads logs/tier2_queue.jsonl), starting at "
                             "tier 2. Clears the queue file when done.")
    parser.add_argument("--no-advisor", action="store_true",
                        help="Skip the qwen advisor call after each failed attempt. "
                             "Recommended for --deep runs: the advisor loads the 7B "
                             "model which evicts the 32B from GPU, adding 20-30s of "
                             "reload overhead per retry.")
    parser.add_argument("--commit-sprint", action="store_true",
                        help="Commit and push sprint planning artifacts "
                             "(ROADMAP.md, task_graph.json, .roorules) before "
                             "starting the work loop. Use after plan_week.py "
                             "so cloud workers can immediately pull the DAG.")
    parser.add_argument("--remaining-count", choices=["features", "tests", "all"], default=None,
                        help="Print the number of unchecked ROADMAP.md tasks matching this "
                             "type and exit. Used by supervisor.sh to check whether a "
                             "features-only or tests-only session still has work, without "
                             "ever mixing the two loop conditions together.")
    parser.add_argument("--queue-remaining-count", choices=["features", "tests", "all"], default=None,
                        help="Same as --remaining-count but counts logs/tier2_queue.jsonl "
                             "entries instead of ROADMAP.md — used for --deep mop-up sessions.")
    return parser


def _resolve_project_root(args) -> str:
    if args.project:
        project_root = os.path.abspath(args.project)
        if not os.path.isdir(project_root):
            print(f"⚠  Not found: {project_root}")
            sys.exit(1)
        os.chdir(project_root)
    else:
        project_root = os.getcwd()
    return project_root


def _pre_run_guards(project_root: str) -> None:
    # ── Pre-run git hygiene guards ───────────────────────────────────────────
    # Skipped for the pure counting queries (--remaining-count /
    # --queue-remaining-count): supervisor.sh captures their stdout as an
    # integer, so these must print nothing and never exit early.
    #   1. Must be on the integration branch (default: main). Starting a run
    #      from a leftover task-* branch is what stranded every earlier fix on
    #      an abandoned branch and left main behind. Override with MAIN_BRANCH.
    #   2. Working tree must be clean, EXCEPT for logs/ (generated each run and
    #      gitignored per project — they must never block or dirty a run).
    if not os.path.exists(os.path.join(project_root, ".git")):
        return
    # Use the resolved config value, NOT a second os.getenv: config.py
    # already layers env over the profile's [run] main_branch over "main".
    # Reading the env alone here meant a profile that set main_branch
    # satisfied config.MAIN_BRANCH and then failed this guard anyway.
    main_branch = MAIN_BRANCH
    cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], project_root).stdout.strip()
    if cur and cur != main_branch:
        print(f"{RED}✗ On branch '{cur}', not '{main_branch}'.{RESET}")
        print(f"  Runs must start from '{main_branch}' so task branches merge back cleanly.")
        print(f"  Leftover task branch? Merge or delete it, then:")
        print(f"    git checkout {main_branch}")
        print(f"  (override the expected branch with MAIN_BRANCH=<name>)")
        sys.exit(1)
    r = _git(["status", "--porcelain"], project_root)
    dirty = [ln for ln in r.stdout.strip().split('\n')
             if ln and 'logs/' not in ln]
    if dirty:
        print(f"{RED}✗ Working tree is dirty. Commit or stash changes before running work:{RESET}")
        print('\n'.join(dirty))
        print(f"\n  git add . && git commit -m 'checkpoint'")
        sys.exit(1)


def _load_config_and_preflight(project_root: str) -> None:
    # ── Load per-project config (.sovereign_config.json) ─────────────────────
    # Must happen before anything else so LOCKED_FILES is populated before
    # any file reads, writes, or planning steps touch the project.
    # EXCEPT for the counting queries: supervisor.sh does
    # `N=$(work.py --remaining-count ...)` and tests N as an integer, so
    # stdout must contain ONLY the number. _load_project_config prints
    # "Loaded N always-include context file(s)" etc., which polluted the
    # capture and made supervisor.sh's [ -eq ] test fail
    # ("integer expression expected") — it then fell through to a false
    # "All ROADMAP tasks complete". Counting uses only parse_all_tasks()/
    # _is_test_task(), neither of which needs the config, so main() skips
    # this for the counting queries. (2026-07-14)
    _load_project_config(project_root)

    if state.RAG_ENABLED:
        try:
            import rag_failures
            n_mined = rag_failures.mine_failure_fixes(project_root)
            if n_mined:
                print(f"  {DIM}RAG: mined {n_mined} new failure->fix pattern(s) from task_traces.jsonl{RESET}")
        except Exception as exc:
            print(f"  {YELLOW}(RAG failure-mining skipped: {exc}){RESET}")

    # ── Environment preflight ────────────────────────────────────────
    # 2026-07-13: 76 consecutive failures on a project where
    # `package:flutter/material.dart` did not resolve. No model can fix an
    # unbuilt dependency tree; every attempt was doomed before the first
    # token. One stat() up front turns a wasted run into a clear message.
    try:
        _problems = grounders.for_language(state.PROJECT_LANGUAGE).preflight(project_root)
        for _p in _problems:
            print(f"  {YELLOW}⚠  PREFLIGHT: {_p}{RESET}")
        if _problems:
            print(f"  {YELLOW}   Fix the above before running — model "
                  f"attempts cannot succeed against a broken tree.{RESET}")
            _ledger(project_root, -1, "-", "preflight_failed",
                    "; ".join(_problems))
    except Exception:
        pass  # preflight is advisory; never block a run on it


def _answer_count_query(args, project_root: str) -> None:
    # ── Fast counting queries (--remaining-count / --queue-remaining-count) ───
    # Pure reads, no Firestore/model calls — supervisor.sh shells out to these
    # on every loop iteration to decide whether a features-only or tests-only
    # session is actually done, so they need to reuse the exact same
    # parse_all_tasks()/_is_test_task() logic the real run uses. Exits before
    # any other side effect (Firestore pull, sprint commit, model checks).
    if args.remaining_count:
        all_tasks = parse_all_tasks()
        if args.remaining_count == "all":
            n = len(all_tasks)
        else:
            want_test = (args.remaining_count == "tests")
            n = sum(1 for line in all_tasks if _is_test_task(task_text(line)) == want_test)
        print(n)
        sys.exit(0)

    if args.queue_remaining_count:
        queue_path = os.path.join(project_root, TIER2_QUEUE_FILE)
        n = 0
        if os.path.exists(queue_path):
            for line in open(queue_path):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if args.queue_remaining_count == "all":
                    n += 1
                elif (args.queue_remaining_count == "tests") == _is_test_task(rec.get("task", "")):
                    n += 1
        print(n)
        sys.exit(0)


def _ensure_flutter_deps(project_root: str, worker_id: int) -> None:
    # ── Dependency check ──────────────────────────────────────────────────────
    # Run flutter pub get if pubspec.yaml is newer than pubspec.lock, or if
    # .dart_tool/package_config.json is missing (fresh clone / stale cache).
    pubspec_yaml = os.path.join(project_root, "pubspec.yaml")
    pubspec_lock = os.path.join(project_root, "pubspec.lock")
    pkg_config   = os.path.join(project_root, ".dart_tool", "package_config.json")
    if not os.path.exists(pubspec_yaml):
        return
    needs_pub_get = (
        not os.path.exists(pkg_config)
        or not os.path.exists(pubspec_lock)
        or os.path.getmtime(pubspec_yaml) > os.path.getmtime(pubspec_lock)
    )
    if not needs_pub_get:
        return
    if worker_id == 0:
        print(f"  {YELLOW}pubspec.yaml changed or cache missing — running flutter pub get...{RESET}")
        result = subprocess.run(
            ["flutter", "pub", "get"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            print(f"  {GREEN}✓ flutter pub get succeeded{RESET}")
        else:
            print(f"  {RED}⚠ flutter pub get failed (continuing anyway):\n{result.stderr[:400]}{RESET}")
    else:
        print(f"  {YELLOW}worker {worker_id}: skipping flutter pub get (worker 0 handles it){RESET}")


def _setup_parallel_worker(args, project_root: str, worker_suffix: str) -> str:
    """Returns the directory this worker operates in (its worktree, when parallel)."""
    # ── Parallel worker config ────────────────────────────────────────────────
    # When stride > 1 each worker handles its own chains of tasks (see
    # sov/scheduling.py). Per-worker suffixes keep log and escalation files
    # separate, and each worker gets its own git worktree (see sov/gitops.py).
    if args.stride > 1:
        state.ESCALATE_LOG = f"logs/escalate{worker_suffix}.md"
        if GIT_BRANCHES and os.path.isdir(os.path.join(project_root, ".git")):
            state.INTEGRATION_ROOT = project_root
            project_root = _setup_worker_worktree(project_root, args.worker_id)
            os.chdir(project_root)
    return project_root


def _resolve_tier_range(args) -> tuple[int, int | None]:
    # ── Quick / Deep mode resolution ──────────────────────────────────────────
    # --quick  → tiers 1-2 only (<30B models); T3 35B and T4 32B are excluded
    # --deep   → only process tasks from tier2_queue.jsonl, starting at tier 4 (32B dense + Claude)
    # --max-tier N → explicit cap (overrides --quick when both given)
    if args.quick and not args.max_tier:
        args.max_tier = QUICK_MAX_TIER_IDX  # derived from MODEL_PARAMS; enforces <30B policy
    run_max_tier_idx: int | None = (
        min(args.max_tier, len(TIER_MODELS)) if args.max_tier else None
    )
    if args.start_tier:
        run_start_tier_idx: int = min(args.start_tier - 1, len(TIER_MODELS) - 1)
    else:
        run_start_tier_idx: int = 1 if args.quick else 0   # quick skips T1
    return run_start_tier_idx, run_max_tier_idx


def _read_queued_tasks(queue_file: str) -> set[str]:
    queued: set[str] = set()
    with open(queue_file) as _qf:
        for _line in _qf:
            try:
                queued.add(json.loads(_line)["task"])
            except (json.JSONDecodeError, KeyError):
                pass
    return queued


def _select_tasks(args, project_root: str, run_start_tier_idx: int,
                  run_max_tier_idx: int | None) -> tuple[list[str], int]:
    """Unchecked ROADMAP task lines this run should consider, and the
    (possibly --deep-adjusted) start tier. May set args.max_tier and
    args.budget_multiplier for --deep, as before."""
    tasks = parse_all_tasks()

    # ── Quick mode: skip tasks already queued for --deep ──────────────────────
    # If tier2_queue.jsonl exists from a prior T1 run, those tasks already
    # failed at tier1. Only skip them if we're re-running from T1 (start=0) —
    # if we're starting at T2+, those queued tasks ARE what we want to process.
    if (args.quick or run_max_tier_idx is not None) and run_start_tier_idx == 0:
        _prior_queue_file = os.path.join(project_root, TIER2_QUEUE_FILE)
        if os.path.exists(_prior_queue_file):
            _already_queued = _read_queued_tasks(_prior_queue_file)
            if _already_queued:
                _before = len(tasks)
                tasks = [t for t in tasks if task_text(t) not in _already_queued]
                _skipped_n = _before - len(tasks)
                if _skipped_n:
                    print(f"  {DIM}Skipping {_skipped_n} task(s) already queued "
                          f"for --deep run{RESET}")

    if args.deep:
        queue_file = os.path.join(project_root, TIER2_QUEUE_FILE)
        if not os.path.exists(queue_file):
            print(f"{YELLOW}No tier2 queue found at {queue_file}.{RESET}")
            print(f"Run with --quick first to populate it.")
            sys.exit(0)
        queued = _read_queued_tasks(queue_file)
        tasks = [t for t in tasks if task_text(t) in queued]
        if not tasks:
            print(f"{GREEN}✓ No queued tasks remaining — all done.{RESET}")
            # Clear stale queue file
            open(queue_file, "w").close()
            sys.exit(0)
        run_start_tier_idx = 1   # start at tier2 — gemma4 (0-indexed)
        if not args.max_tier:
            args.max_tier = 3   # cap at tier3 — qwen3.6; T4 32B reserved for explicit --max-tier 4
        # Deep mode gets a generous budget by default (2× normal multiplier)
        if args.budget_multiplier is None:
            args.budget_multiplier = BUDGET_MULTIPLIER * 2

    # NOTE (2026-07-10): same-file tasks used to be merged into one giant
    # "[COMPOUND — implement ALL N sub-tasks...]" LLM call. Removed — bundling
    # N small tasks into one oversized generation call directly fights the
    # "smallest possible task" principle, and empirically made tasks HARDER
    # to complete, not safer (a 5-subtask compound call on one file failed
    # across all 4 tiers for over an hour, when 5 separate small edits would
    # each have had a real chance). Same-file tasks stay small and separate;
    # chain-building (sov/scheduling.py) still keeps them on one worker, in
    # order, and the same-file cascade guard in _first_pass replaces the
    # safety compound-merging used to provide.
    return tasks, run_start_tier_idx


def _print_banner(args, today_str: str, indexed_tasks: list,
                  run_start_tier_idx: int, run_max_tier_idx: int | None) -> None:
    mode_label = f"  ⚡ QUICK (tiers 2–{QUICK_MAX_TIER_IDX}, <{QUICK_PARAM_LIMIT_B:.0f}B)" if args.quick else (
                 "  🔬 DEEP (tiers 2–3 from queue)" if args.deep else "")
    worker_label = f" · worker {args.worker_id}/{args.stride}" if args.stride > 1 else ""
    tier_range = (f"tier{run_start_tier_idx + 1}–{run_max_tier_idx or len(TIER_MODELS)}"
                  if (run_start_tier_idx or run_max_tier_idx) else
                  f"{len(TIER_MODELS)} tiers")
    print(f"{BOLD}{'━'*56}{RESET}")
    print(f"{BOLD}  🤖  Autonomous work loop — {today_str}{worker_label}{RESET}")
    if mode_label:
        print(f"{BOLD}{mode_label}{RESET}")
    print(f"{BOLD}  {len(indexed_tasks)} tasks · {tier_range} · "
          f"{PHASE_MAX_ATTEMPTS}/{TIER2_MAX_ATTEMPTS}/{TIER4_MAX_ATTEMPTS} attempts "
          f"(tier1/mid/last) · YOLO{RESET}")
    print(f"{BOLD}{'━'*56}{RESET}\n")


def _verify_models(run_start_tier_idx: int, run_max_tier_idx: int | None) -> None:
    # ── Verify Model Availability ─────────────────────────────────────────────
    # Fast check at startup to ensure the cascade won't fail halfway through a run
    print(f"  {DIM}Verifying model availability...{RESET}")
    # Only check models that will actually be used in this run
    active_tiers = TIER_MODELS[run_start_tier_idx:(run_max_tier_idx or len(TIER_MODELS))]
    required_models = active_tiers + [PLANNER_MODEL, ADVISOR_MODEL]
    if RACE_ENABLED and run_start_tier_idx == 0:
        required_models.append(RACE_MODEL)  # racing tier1 vs RACE_MODEL — must be pulled too
    preflight_models(required_models)
    _unique = list(dict.fromkeys(required_models))
    if len(_unique) == 1:
        print(f"  {DIM}Model ready: {_unique[0]}{RESET}\n")
    else:
        print(f"  {DIM}All {len(_unique)} models ready.{RESET}\n")


def _go_task_deps(indexed_tasks: list[tuple[int, str]]) -> dict[int, list[tuple[int, str]]]:
    # ── Go dependency gating (2026-07-15) ─────────────────────────────────────
    # 2026-07-15's run burned ~20 attempts on level/seed.go hallucinating
    # Level-struct fields — because level/level.go (which CREATES type Level)
    # had failed minutes earlier. A task whose prerequisite failed this run
    # cannot possibly compile; detect it and send it straight to the retry
    # pass. Two conservative rules:
    #   1. types — task A says "implement types X, Y"; a later task whose text
    #      mentions X or Y depends on A.
    #   2. package qualifier — task text using `pkg.Ident` (e.g. `game.Tick`)
    #      depends on earlier tasks targeting files under pkg/.
    go_task_deps: dict[int, list[tuple[int, str]]] = {}
    if state.PROJECT_LANGUAGE != "go":
        return go_task_deps
    _go_tgt_re = re.compile(r'\bIn ([\w./-]+\.go):')
    _decl_re = re.compile(r'implement types?\s+([A-Z]\w*(?:\s*,\s*[A-Z]\w*)*)')
    _entries = []
    for _ti, _tl in indexed_tasks:
        _tt = task_text(_tl)
        _m = _go_tgt_re.search(_tt)
        _dm = _decl_re.search(_tt)
        _decls = {d.strip() for d in _dm.group(1).split(",")} if _dm else set()
        _entries.append((_ti, _m.group(1) if _m else None, _decls, _tt))
    for _i, (_ti, _tgt, _, _tt) in enumerate(_entries):
        _deps = []
        _pkg_refs = set(re.findall(r'\b([a-z]\w+)\.[A-Z]\w*', _tt)) - {"sim"}
        for _pi, _ptgt, _pdecls, _ptt in _entries[:_i]:
            if not _ptgt:
                continue
            if any(re.search(r'\b' + re.escape(d) + r'\b', _tt) for d in _pdecls):
                _deps.append((_pi, _ptgt))
                continue
            _ppkg = _ptgt.split("/")[0]
            if _ppkg in _pkg_refs and not (_tgt or "").startswith(_ppkg + "/"):
                _deps.append((_pi, _ptgt))
        if _deps:
            go_task_deps[_ti] = _deps
    return go_task_deps


def _merge_or_requeue(result, branch: str | None, project_root: str,
                      task_idx: int, task: str):
    """Merge a passing task's branch. A parallel merge conflict downgrades the
    result to "skipped" so the task is retried rather than marked done."""
    if result is True and branch:
        if _branch_merge(project_root, branch, task_idx, task):
            print(f"  {DIM}⎇  Merged {branch} → main{RESET}")
        else:
            result = "skipped"  # parallel merge conflict — retry next pass
    return result


def _first_pass(args, indexed_tasks, project_root: str, log_file: str,
                get_first_pass_budget, run_start_tier_idx: int,
                run_max_tier_idx: int | None):
    """Run every assigned task once. Returns (passed, failed, skipped_tasks,
    retry_queue)."""
    passed_count  = 0
    failed_count  = 0
    attempted     = 0                                      # tasks actually handed to a model
    skipped_tasks: list[tuple[int, str]] = []              # (task_idx, task_text)
    retry_queue:   list[tuple[int, str, str]] = []         # (task_idx, task_line, task_text)

    # Dependency tracking: when a task fails/skips, its immediate successor is
    # auto-blocked and queued for the retry pass instead of running against a
    # broken/missing implementation, in two cases:
    #   (a) the successor is a test task (would test something that isn't there)
    #   (b) the successor targets the SAME primary file (would inherit whatever
    #       broken state the failed task left the file in)
    # (b) replaces the old compound-merge behavior — same-file tasks stay
    # small and separate now, but still can't cascade a failure forward.
    last_incomplete: int | None = None       # task index of last failed/skipped task
    last_incomplete_file: str | None = None  # its primary file, if any

    go_failed_targets: set[str] = set()
    go_task_deps = _go_task_deps(indexed_tasks)
    strong_run = routing.is_strong_run(
        project_root, TIER_MODELS[run_start_tier_idx:(run_max_tier_idx or len(TIER_MODELS))], MODEL_PARAMS)

    for loop_idx, (task_idx, task_line) in enumerate(indexed_tasks, 1):
        if loop_idx < args.start_at:
            print(f"{DIM}[{loop_idx}/{len(indexed_tasks)}] Skipping: {task_text(task_line)[:60]}{RESET}")
            continue

        task = task_text(task_line)
        is_test = _is_test_task(task)

        # ── Feature/Test filtering ───────────────────────────────────────────
        if args.features_only and is_test:
            print(f"  {DIM}[deferred] Task {task_idx} is a test task (features-only mode){RESET}")
            continue
        if args.tests_only and not is_test:
            print(f"  {DIM}[skipping] Task {task_idx} is a feature task (tests-only mode){RESET}")
            continue

        print(f"\n{BOLD}[{loop_idx}/{len(indexed_tasks)}] {task[:70]}{'...' if len(task) > 70 else ''}{RESET}")

        with open(log_file, "a") as f:
            f.write(f"\n## Task {task_idx}: {task}\n")

        if args.dry_run:
            print(f"  {DIM}(dry-run){RESET}")
            continue

        # Routing policy (opt-in, sov/routing.py): a small-model run leaves a
        # new layer's first tasks, and tasks small models keep failing, to the
        # strong pass instead of spending attempts on them.
        _why = routing.check(task, project_root,
                             TIER_MODELS[run_start_tier_idx:(run_max_tier_idx or len(TIER_MODELS))],
                             MODEL_PARAMS)
        if _why:
            print(f"  {DIM}[deferred] {_why} — left for the strong pass{RESET}")
            with open(log_file, "a") as f:
                f.write(f"Deferred by routing: {_why}\n")
            continue

        # Go dependency gating: a prerequisite task failed this run, so this
        # task cannot compile no matter what the model writes — don't burn
        # attempts proving it. Straight to the retry pass (where the
        # prerequisite retries first, in queue order).
        _unmet = [(di, dt) for di, dt in go_task_deps.get(task_idx, [])
                  if dt in go_failed_targets]
        if _unmet:
            _di, _dt = _unmet[0]
            print(f"  {YELLOW}⛓  Dependency not met: task {_di} ({_dt}) failed this run "
                  f"— deferred to retry pass{RESET}")
            skipped_tasks.append((task_idx, task))
            retry_queue.append((task_idx, task_line, task))
            last_incomplete = task_idx
            last_incomplete_file = _task_primary_file(task_line)
            _ledger(project_root, task_idx, "-", "task_dep_blocked", _dt)
            continue

        # Dependency block: defer this task if the immediately preceding task
        # didn't complete AND either (a) this is ITS test (same feature stem —
        # not just any test task), or (b) this targets the same file (would
        # inherit whatever broken state was left behind).
        #
        # Was `_is_test_task(task)` alone for (a), which blocks ANY adjacent
        # test task regardless of what it actually tests. Harmless in an
        # interleaved feature+test ROADMAP (the very next task usually is the
        # matching test), but in a --tests-only session every task is a test
        # task — so one genuinely stuck task (observed: chrome_knob_test, a
        # broken gesture-simulation spec, not a model error) silently chained
        # "dependency-blocked" through all 10 unrelated remaining tests for
        # hours, none of them ever actually attempted. Stem-matching preserves
        # the intended same-feature pairing while ending that blast radius.
        is_immediate_successor = last_incomplete is not None and task_idx == last_incomplete + 1
        same_file_as_failure = (
            is_immediate_successor
            and last_incomplete_file is not None
            and _task_primary_file(task_line) == last_incomplete_file
        )
        same_feature_test = (
            is_immediate_successor
            and _is_test_task(task)
            and _feature_stem(_task_primary_file(task_line)) is not None
            and _feature_stem(_task_primary_file(task_line)) == _feature_stem(last_incomplete_file)
        )
        if is_immediate_successor and (same_feature_test or same_file_as_failure):
            reason = "same file as the failed task" if same_file_as_failure else "test of the feature that failed"
            print(
                f"  {YELLOW}⏭  Dependency-blocked: task {last_incomplete} did not complete "
                f"({reason}) — deferred to retry pass{RESET}"
            )
            with open(log_file, "a") as f:
                f.write(f"Dependency-blocked: preceding task {last_incomplete} failed/skipped ({reason}).\n")
            skipped_tasks.append((task_idx, task))
            retry_queue.append((task_idx, task_line, task))
            last_incomplete = task_idx   # chain: mark this task as incomplete too
            last_incomplete_file = _task_primary_file(task_line)
            continue

        branch = _branch_start(project_root, task_idx, task)
        if branch:
            print(f"  {DIM}⎇  Branch: {branch}{RESET}")

        attempted += 1
        if strong_run:
            # Count the strike BEFORE the attempt and clear it on a pass: a task
            # the pass cap kills mid-attempt still leaves its strike. Otherwise
            # compute_window timed out cycle after cycle with a clean record
            # and could never be parked (2026-09-30).
            routing.begin_strong_attempt(project_root, task)
        result = run_task(task, project_root, log_file, task_idx=task_idx,
                         budget_s=get_first_pass_budget(),
                         max_tier_idx=run_max_tier_idx,
                         start_tier_idx=run_start_tier_idx,
                         skip_advisor=getattr(args, 'no_advisor', False))
        result = _merge_or_requeue(result, branch, project_root, task_idx, task)
        if strong_run:
            if state.LAST_TASK_DEFERRED:
                routing.forgive_inflight(project_root)     # deferred, never attempted
            else:
                routing.end_strong_attempt(project_root, task, result is True)

        _tm = re.search(r'\bIn ([\w./-]+\.go):', task)
        if result is True:
            mark_done(task_line)
            print(f"  {GREEN}✓ Marked done in ROADMAP.md{RESET}")
            passed_count += 1
            if _tm:
                go_failed_targets.discard(_tm.group(1))
            last_incomplete = None        # successful task resets the chain
            last_incomplete_file = None
            continue

        if branch:
            _branch_abandon(project_root, branch)
        retry_queue.append((task_idx, task_line, task))
        last_incomplete = task_idx
        last_incomplete_file = _task_primary_file(task_line)
        if _tm:
            go_failed_targets.add(_tm.group(1))
        if result == "skipped":
            # In a capped run (--quick / --max-tier), write failures to the
            # tier2 queue so a follow-up --deep run can pick them up.
            if run_max_tier_idx is not None and run_max_tier_idx < len(TIER_MODELS):
                _write_tier2_queue(project_root, task_idx, task, [])
                print(f"  {YELLOW}↳ queued for --deep run{RESET}")
            skipped_tasks.append((task_idx, task))
            print(f"  {YELLOW}⏭  Skipped — queued for retry pass{RESET}")
        else:
            failed_count += 1
            print(f"  {YELLOW}Continuing to next task...{RESET}")

    return passed_count, failed_count, skipped_tasks, retry_queue, attempted


def _retry_pass(args, retry_queue, project_root: str, log_file: str,
                session_start: float) -> None:
    # ── Automatic retry pass ──────────────────────────────────────────────────
    retry_budget = compute_task_budget(
        project_root,
        multiplier=BUDGET_MULTIPLIER * RETRY_BUDGET_MULT,
        ceiling_s=BUDGET_CEILING_S * RETRY_BUDGET_MULT,   # 10 min × 2 = 20 min
    )
    print(f"\n{BOLD}{'━'*56}{RESET}")
    print(f"{BOLD}  🔁  Retry pass — {len(retry_queue)} task(s) · "
          f"budget {retry_budget:.0f}s ({retry_budget/60:.1f} min) each{RESET}")
    print(f"{BOLD}{'━'*56}{RESET}\n")

    retry_passed = retry_failed = retry_skipped = 0
    for task_idx, task_line, task in retry_queue:
        print(f"\n{BOLD}[retry {task_idx}] "
              f"{task[:70]}{'...' if len(task) > 70 else ''}{RESET}")
        with open(log_file, "a") as lf:
            lf.write(f"\n## Retry Task {task_idx}: {task}\n")

        # 2026-07-14: the retry pass MUST use the same branch isolation as
        # the first pass. Without it, successful retries were marked done
        # while their files sat uncommitted in the working tree, and failed
        # retries left broken files behind that made `go vet ./...` fail
        # for every subsequent task (identical cascading error blocks).
        branch = _branch_start(project_root, task_idx, task)
        if branch:
            print(f"  {DIM}⎇  Branch: {branch}{RESET}")

        retry_strong = routing.is_strong_run(project_root, TIER_MODELS, MODEL_PARAMS)
        if retry_strong:
            routing.begin_strong_attempt(project_root, task)   # see the first pass
        result = run_task(task, project_root, log_file,
                          task_idx=task_idx, budget_s=retry_budget,
                          skip_advisor=getattr(args, 'no_advisor', False))
        result = _merge_or_requeue(result, branch, project_root, task_idx, task)
        if retry_strong:
            if state.LAST_TASK_DEFERRED:
                routing.forgive_inflight(project_root)
            else:
                routing.end_strong_attempt(project_root, task, result is True)
        if result is True:
            mark_done(task_line)
            print(f"  {GREEN}✓ Marked done in ROADMAP.md{RESET}")
            retry_passed += 1
        elif result == "skipped":
            if branch:
                _branch_abandon(project_root, branch)
            retry_skipped += 1
            print(f"  {YELLOW}⏭  Still skipped after retry{RESET}")
        else:
            if branch:
                _branch_abandon(project_root, branch)
            retry_failed += 1
            print(f"  {RED}✗ Still failing after retry{RESET}")

    print(f"\n{BOLD}{'━'*56}{RESET}")
    print(f"  Retry pass: {GREEN}✓ {retry_passed} recovered{RESET}  "
          f"{YELLOW}⏭ {retry_skipped} still skipped{RESET}  "
          f"{RED}✗ {retry_failed} still failing{RESET}")
    print(f"{BOLD}{'━'*56}{RESET}\n")
    print_timing_report(project_root, session_start)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Interrupted — progress saved to ROADMAP.md{RESET}\n")
        sys.exit(130)
