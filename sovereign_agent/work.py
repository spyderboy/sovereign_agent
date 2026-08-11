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
import os
import re
import sys
import json
import time
import fcntl
import shutil
import argparse
import subprocess
import contextlib
import import_fixer
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
import requests
from requests.exceptions import (
    ReadTimeout, ConnectionError as RequestsConnectionError, HTTPError
)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))


class PromptTooLargeError(Exception):
    """Raised when Ollama reports the prompt was truncated to fit the context window.

    Signals that the caller should either trim the context (fewer files) or
    queue the task for a run with a larger context window (--deep pass).
    """
    def __init__(self, token_count: int, limit: int):
        self.token_count = token_count
        self.limit = limit
        super().__init__(
            f"Prompt truncated: {token_count} tokens used {token_count/limit*100:.0f}% "
            f"of {limit}-token context window"
        )


OLLAMA_URL      = os.getenv("LOCAL_MODEL_URL",  "http://localhost:11434")
# Integration branch. Tasks fork from and merge back to this. Override for
# projects whose integration branch isn't 'main' (e.g. galaxican uses 'master').
# The branch helpers below MUST use this, not a hardcoded 'main', or work merges
# to a divergent 'main' while the real integration branch is left behind.
MAIN_BRANCH     = os.getenv("MAIN_BRANCH", "main")
TIER1_MODEL     = os.getenv("TIER1_MODEL",      "qwen2.5-coder:7b-instruct-q4_K_M")  # 7B dense  — fast, strong on Dart
TIER2_MODEL     = os.getenv("TIER2_MODEL",      "gemma4:26b")                          # 26B MoE (4B active) — quick second opinion
TIER3_MODEL     = os.getenv("TIER3_MODEL",      "qwen3-coder:30b")                    # 30B dense — Qwen3, spatial reasoning
TIER4_MODEL     = os.getenv("TIER4_MODEL",      "qwen2.5-coder:32b")                  # 32B dense — heavy hitter before Claude
TIER_MODELS     = [TIER1_MODEL, TIER2_MODEL, TIER3_MODEL, TIER4_MODEL]   # Claude handles final escalation

# ── Quick-mode parameter gate ─────────────────────────────────────────────────
# POLICY: --quick must only use models whose total parameter count is < 30B.
# This is enforced at runtime by QUICK_MAX_TIER_IDX, derived from MODEL_PARAMS.
# When adding or swapping a model, update MODEL_PARAMS with its total param count
# (in billions, as a float). Unknown models default to float('inf') → excluded.
# Override individual entries via env: MODEL_PARAMS_TIER1=7 etc.
QUICK_PARAM_LIMIT_B: float = float(os.getenv("QUICK_PARAM_LIMIT_B", "30"))

MODEL_PARAMS: dict[str, float] = {
    TIER1_MODEL:   float(os.getenv("MODEL_PARAMS_TIER1",   "7")),    #  7B dense
    TIER2_MODEL:   float(os.getenv("MODEL_PARAMS_TIER2",   "26")),   # 26B MoE
    TIER3_MODEL:   float(os.getenv("MODEL_PARAMS_TIER3",   "30")),   # 30B dense
    TIER4_MODEL:   float(os.getenv("MODEL_PARAMS_TIER4",   "32")),   # 32B dense
}

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

QUICK_MAX_TIER_IDX: int = _quick_max_tier_idx(TIER_MODELS, MODEL_PARAMS, QUICK_PARAM_LIMIT_B)
RACE_MODEL      = os.getenv("RACE_MODEL",       "qwen2.5-coder:7b-instruct-q4_K_M")  # kept for future A/B experiments
RACE_ENABLED    = os.getenv("RACE_ENABLED", "0") == "1"   # off by default; enable with RACE_ENABLED=1
GIT_BRANCHES    = os.getenv("GIT_BRANCHES",  "1") == "1"  # branch-per-task; disable with GIT_BRANCHES=0
PLANNER_MODEL   = os.getenv("PLANNER_MODEL",    "qwen2.5-coder:7b-instruct-q4_K_M")
ADVISOR_MODEL   = os.getenv("ADVISOR_MODEL",    "qwen2.5-coder:7b-instruct-q4_K_M")

# Claude API escalation (final tier after all local models exhausted)
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_ENABLED      = os.getenv("CLAUDE_ENABLED", "0") == "1"   # opt-in only; set CLAUDE_ENABLED=1 to re-enable

# ── Per-model context windows ─────────────────────────────────────────────────
# Larger context = more files visible per task, but more VRAM for KV cache.
# Sized for M4 32 GB: small models get more context since their weights are cheaper.
# Override any entry via env vars (e.g. CTX_TIER1=16384).
#
# IMPORTANT: this dict is keyed by MODEL NAME, not by role. TIER1_MODEL,
# PLANNER_MODEL, and ADVISOR_MODEL are the same model string by default
# (qwen2.5-coder:7b-instruct-q4_K_M), so CTX_TIER1/CTX_PLANNER/CTX_ADVISOR are
# NOT three independent settings — a Python dict literal silently lets the
# last duplicate key win, so only ONE of them actually takes effect for all
# three roles (whichever line is listed last below). Keep all three env vars
# equal unless TIER1/PLANNER/ADVISOR are ever pointed at genuinely different
# models. Fixed 2026-07-10: previously defaulted to 32768 while RACE_MODEL
# defaulted to 65536 — an unfair race, and backwards, since qwen2.5-coder:7b
# (4.7GB) is the SMALLEST model in the whole lineup, smaller than
# gemma4:12b-mlx (7.7GB) which was getting the bigger context window.
MODEL_CTX: dict[str, int] = {
    TIER1_MODEL:   int(os.getenv("CTX_TIER1",   "65536")),  # 7B dense  — smallest model, most headroom
    TIER2_MODEL:   int(os.getenv("CTX_TIER2",   "32768")),  # 26B MoE   — DOUBLED 2026-07-11: same
    # silent-overflow suspect as tier4 (fixed below) — tier2/3 "no output" failures
    # were untraceable until the trace-logging gap was closed this session; real
    # prompts run in the same 14-16k token range that overflowed tier4 at 16384.
    TIER3_MODEL:   int(os.getenv("CTX_TIER3",   "32768")),  # 35B MoE   — DOUBLED 2026-07-11, same reasoning
    # 2026-07-10: doubled 16384→32768 — this was the "last resort" tier failing
    # on every single escalated task in a real run, hitting "prompt too large"
    # even after trimming to 3 files (real prompts were running 14.5k-15.7k
    # tokens). A tier that can't accept the prompt isn't a safety net, it's a
    # guaranteed dead end. Watch VRAM on 32GB Macs — this is the one tier where
    # the ceiling really was sized tight on purpose, so if this causes OOM
    # thrashing it may need to come back down and get its file-trimming logic
    # tightened instead.
    TIER4_MODEL:   int(os.getenv("CTX_TIER4",   "32768")),  # 32B dense
    PLANNER_MODEL: int(os.getenv("CTX_PLANNER", "65536")),  # same model as TIER1_MODEL — see note above
    ADVISOR_MODEL: int(os.getenv("CTX_ADVISOR", "65536")),  # same model as TIER1_MODEL — see note above
    # RACE_MODEL wasn't in this table before (2026-07-10) — it silently fell through
    # to the OLLAMA_CONTEXT_LENGTH default of 16384, which caused it to truncate on
    # ~31% of race attempts (compound/multi-file prompts ran 91-107% of that limit).
    # Now matches TIER1_MODEL's 65536 so the two raced models get equal footing.
    RACE_MODEL:    int(os.getenv("CTX_RACE",    "65536")),
}

# The TIER1/PLANNER/ADVISOR collision above is silent by design (a dict can only
# hold one value per key) — this check makes it loud instead, in case someone
# sets the env vars inconsistently expecting three independent values.
if len({TIER1_MODEL, PLANNER_MODEL, ADVISOR_MODEL}) == 1:
    _ctx_vals = {os.getenv("CTX_TIER1", "65536"), os.getenv("CTX_PLANNER", "65536"), os.getenv("CTX_ADVISOR", "65536")}
    if len(_ctx_vals) > 1:
        print(f"⚠  CTX_TIER1/CTX_PLANNER/CTX_ADVISOR are set to different values "
              f"({_ctx_vals}) but TIER1_MODEL/PLANNER_MODEL/ADVISOR_MODEL are the "
              f"same model ({TIER1_MODEL}) — only one value actually applies "
              f"(MODEL_CTX[{TIER1_MODEL!r}] = {MODEL_CTX[TIER1_MODEL]}). "
              f"Set all three env vars to the same value.")

# Strikes before advancing to the next tier (same error repeated)
PHASE_STRIKE_LIMIT = 2

# Hard cap on total validation failures per tier — catches thrashing (all-different errors).
# 2026-07-10: tier 1 lowered from 6→3 — a task that isn't solved in 3 tries on the fast,
# cheap tier is better off escalating sooner than grinding. Tiers 2-3 stay tight at 2 (they
# already were). Tier 4 — the LAST tier, the automated system's final resort before a task
# needs a human's attention — gets its own named constant instead of being lumped in with
# tiers 2-3 under one "tier_idx > 0" check, so it can be tuned independently of them.
PHASE_MAX_ATTEMPTS = 3
TIER2_MAX_ATTEMPTS = 2
TIER4_MAX_ATTEMPTS = 2


def _max_attempts_for_tier(idx: int) -> int:
    """Single source of truth for the attempt ceiling — tier 1 / last tier / everything
    in between each get their own budget. Also used for the printed 'Attempt X/N' label,
    which used to always print PHASE_MAX_ATTEMPTS even on tiers enforcing a different cap."""
    if idx == 0:
        return PHASE_MAX_ATTEMPTS
    if idx == len(TIER_MODELS) - 1:
        return TIER4_MAX_ATTEMPTS
    return TIER2_MAX_ATTEMPTS

# ── Time budget ────────────────────────────────────────────────────────────────
# Max wall-clock seconds a single task may run before being skipped.
# Computed dynamically from rolling average of recently completed tasks.
BUDGET_SAMPLES    = 20    # recent completed tasks to include in average
BUDGET_MULTIPLIER = 1.7   # first pass: ~1.7× rolling average (6 min avg → 10 min budget)
BUDGET_FLOOR_S    = 120   # never cut off before 2 min regardless of average
BUDGET_CEILING_S  = 1200  # first-pass hard cap: 20 min
RETRY_BUDGET_MULT = 2.0   # retry pass ceiling: 2× first-pass ceiling = 40 min

# Absolute hard ceiling — no exceptions. Unlike BUDGET_CEILING_S / RETRY_BUDGET_MULT
# (which only apply in the "wrote code, validation failed" path and can be pushed out
# by the +300s "bonus" granted on each tier escalation), this is checked at the very
# top of every attempt, on every code path (timeout, bad-pattern loop, no-output,
# stale-error thrash). 2026-07-12: a single task ran 25,769s (~7.2h) because repeated
# tier escalations each got their own 300s bonus and the bad-pattern/no-output
# branches never consulted the budget check at all. This caps total wall-clock time
# per task regardless of how many tiers remain or how the bonus math works out.
MAX_TASK_SECONDS  = 3600  # 1 hour, hard stop — no bonuses, no tier-availability escape hatch

# Lazy imports — only loaded on first validation failure to avoid startup cost
_autofix = None
_advisor = None

def _get_autofix():
    global _autofix
    if _autofix is None:
        import autofix as _autofix
    return _autofix

def _get_advisor():
    global _advisor
    if _advisor is None:
        import qwen_advisor as _advisor
    return _advisor
MAX_RETRIES    = 50
ROADMAP_PATH   = "ROADMAP.md"
LOG_DIR        = "logs"

BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# All unchecked tasks are worked through in one run — no daily cap.
# Velocity (tasks completed / elapsed time) is logged and used by standup.py
# to project throughput. The standup measures what was done, not what to allow.


# ─── Ollama ───────────────────────────────────────────────────────────────────

def _ollama_installed_tags() -> list[str]:
    """Return the list of model tags Ollama currently has, or raise on failure.

    Raised exceptions are handled by preflight_models() so it can tell an
    Ollama-not-running error apart from a merely-missing model.
    """
    resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


def preflight_models(required: list[str]) -> None:
    """Verify Ollama is up and every required model tag is already pulled.

    Fails fast with actionable guidance instead of silently streaming a
    multi-GB pull mid-setup or dying with an opaque connection error:
      • Ollama unreachable  → tell the user to start it (`ollama serve`).
      • Model tag missing    → print the exact `ollama pull <tag>` command.
    Auto-pulling was removed 2026-07-17: an unattended 18GB download at run
    start was surprising, and a missing tag is nearly always a typo'd model
    name (e.g. qwen3.6:35b vs qwen3-coder:30b) that a pull would only mask.
    """
    try:
        installed = _ollama_installed_tags()
    except Exception as e:
        print(f"  {RED}✗ Ollama is not reachable at {OLLAMA_URL}: {e}{RESET}")
        print(f"    Start it first:  ollama serve")
        sys.exit(1)

    def _present(tag: str) -> bool:
        return tag in installed or f"{tag}:latest" in installed

    missing = [m for m in dict.fromkeys(required) if not _present(m)]
    if missing:
        print(f"  {RED}✗ Required model(s) not pulled in Ollama:{RESET}")
        for m in missing:
            print(f"      ollama pull {m}")
        print(f"    Installed tags: {', '.join(installed) or '(none)'}")
        print(f"    (Tag must match `ollama list` exactly.)")
        sys.exit(1)


_LARGE_MODEL_THRESHOLD_GB = 15  # flush before loading anything this large

# Rough VRAM footprint by model name fragment (GB).
# Used to decide whether to flush before loading a tier3/4 model.
_MODEL_SIZE_HINTS: dict[str, float] = {
    "32b":   19.0,
    "30b":   18.0,
    "r1":    19.0,
    "35b":   20.0,  # qwen3.6:35b-a3b — MoE, full model ~20GB in VRAM
    "26b":   15.0,  # gemma4:26b       — MoE, full model ~15GB in VRAM
    "24b":   14.0,
    "20b":   13.0,
    "16b":    8.9,
    "14b":    9.0,
    "7b":     4.7,
    "4b":     4.0,
}

def _model_size_gb(model_name: str) -> float:
    """Estimate model VRAM footprint from its name."""
    name = model_name.lower()
    for fragment, gb in _MODEL_SIZE_HINTS.items():
        if fragment in name:
            return gb
    return 10.0  # safe default if unknown


# 2026-07-11: cross-process lock so at most one worker at a time runs a
# tier2+ ("large") model. Parallel quick-sweep workers each escalate through
# tiers independently and were never coordinated with each other — only the
# --deep pass (which is exclusively tier2+ retries) had a VRAM guard, and it
# worked by forcing --workers 1 for the whole pass. That's too blunt for the
# quick pass, where most tasks stay on tier1 and losing all parallelism there
# would be a big throughput hit. Instead, tier1 calls stay fully parallel and
# only the large-model calls serialize across workers — same effect as the
# deep-pass guard, scoped to just the calls that actually need it. This
# matters more now that tier2/3/4 context windows were all doubled to 32768
# (2026-07-11), since two of these loaded at once is the exact VRAM-thrash
# scenario the deep-pass guard exists to prevent.
_TIER2_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tier2_lock")


@contextlib.contextmanager
def _large_model_lock():
    with open(_TIER2_LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def ollama(model: str, system: str, user: str, timeout: int = 300) -> str:
    # Flush VRAM before loading any large model to avoid swap on 32GB Mac.
    # Tier1/2 (7b/16b) stay resident together (~13GB combined) — no flush needed.
    # Tier3/4 (32b, r1:32b) need clear headroom — flush everything first.
    keep_alive = -1  # default: keep in memory indefinitely
    size_gb = _model_size_gb(model)
    if size_gb >= _LARGE_MODEL_THRESHOLD_GB:
        # Keep large models alive for 10 min between retries — avoids reloading
        # 19 GB from scratch on every attempt.  With --workers 1 (required for
        # deep pass), this is safe; with --workers 2 the two 32B models still
        # won't coexist (19 GB each), so deep must always use a single worker.
        print(f"  {DIM}Loading {size_gb:.0f}GB model ({model.split(':')[0]}) — keeping resident 30m...{RESET}")
        keep_alive = "30m"

    # ── Pre-flight prompt size check ──────────────────────────────────────────
    # Estimate token count before sending so we never pay for a full inference
    # round-trip on a prompt Ollama will just silently truncate.
    # Code averages ~3.5 chars/token for BPE tokenisers; use 3 to be conservative.
    ctx_limit = MODEL_CTX.get(model, int(os.getenv("OLLAMA_CONTEXT_LENGTH", "16384")))
    estimated_tokens = (len(system) + len(user)) // 3
    if estimated_tokens >= int(ctx_limit * 0.85):
        raise PromptTooLargeError(estimated_tokens, ctx_limit)

    # stream=True: Ollama writes tokens as they're generated, so the server's
    # 10-minute write-deadline never fires on long tasks.  We reassemble the
    # content from newline-delimited JSON chunks — callers see no difference.
    def _do_request() -> str:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "options": {"num_ctx": ctx_limit},
                "stream": True,
                "keep_alive": keep_alive,
            },
            stream=True,
            timeout=timeout,
        )
        resp.raise_for_status()
        content = []
        done_chunk: dict = {}
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                content.append(delta)
            if chunk.get("done"):
                done_chunk = chunk
                break

        # Post-flight confirmation: verify the model didn't truncate despite our
        # estimate (e.g. tokeniser is denser than expected for this content).
        prompt_tokens = done_chunk.get("prompt_eval_count", 0)
        if prompt_tokens and prompt_tokens >= int(ctx_limit * 0.90):
            raise PromptTooLargeError(prompt_tokens, ctx_limit)  # ctx_limit already model-specific

        return "".join(content)

    # Only large (tier2+) models serialize across workers — tier1 stays fully
    # parallel. See _large_model_lock() above for why.
    if size_gb >= _LARGE_MODEL_THRESHOLD_GB:
        with _large_model_lock():
            return _do_request()
    return _do_request()


# ─── JSON extraction helper ───────────────────────────────────────────────────

def _extract_first_json_object(text: str) -> dict:
    """Extract the first complete, balanced JSON object from text.

    Uses a brace-depth scan so trailing prose after the closing brace (or a
    second JSON block) doesn't cause 'Extra data' errors.  Returns {} on any
    parse failure.
    """
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


# ─── ROADMAP helpers ──────────────────────────────────────────────────────────

def parse_all_tasks() -> list[str]:
    """Return every unchecked task from ROADMAP.md, in order, regardless of date.
    Date headers are used only for human readability — they do not gate execution."""
    if not os.path.exists(ROADMAP_PATH):
        return []
    task_re = re.compile(r"^- \[ \] .+")
    return [
        line.strip()
        for line in open(ROADMAP_PATH)
        if task_re.match(line.strip())
    ]


def task_text(line: str) -> str:
    return re.sub(r"^- \[.\] ", "", line).strip()


# ─── Task type classification ─────────────────────────────────────────────────

_TEST_TASK_RE = re.compile(
    r'^(write|add)\s+(unit\s+|widget\s+|golden\s+|integration\s+)?test',
    re.IGNORECASE,
)

def _is_test_task(task: str) -> bool:
    """Return True if this task must only produce files under test/.

    Test tasks must never modify lib/ source files.  If the feature under test
    is missing they should write a skip-marked placeholder instead of
    implementing the feature themselves.
    """
    return bool(_TEST_TASK_RE.match(task.strip()))


def mark_done(task_line: str):
    """Mark a task done in ROADMAP.md. Uses an exclusive file lock so parallel
    workers cannot corrupt each other's writes.

    2026-07-13: the [x] mark MUST be committed immediately. mark_done() runs
    after _branch_merge() returns to main, so the mark sat UNCOMMITTED in the
    working tree — and the very next task's _branch_start() does a forced
    `git checkout -f main`, which resets every tracked file (ROADMAP.md
    included) to HEAD. Net effect: every DONE mark was erased seconds after
    it was written, and the remaining-task count never moved between runs.
    (Before the 2026-07-13 force-checkout fix the marks survived only by
    accident — the dirty tree leaked through plain checkouts and eventually
    got swept into a later task's `git add -A` commit.)
    """
    with open(ROADMAP_PATH, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            lines = f.readlines()
            f.seek(0)
            for line in lines:
                if line.strip() == task_line:
                    f.write(line.replace("- [ ]", "- [x]", 1))
                else:
                    f.write(line)
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    _commit_roadmap_mark(task_line)


def _commit_roadmap_mark(task_line: str, attempts: int = 4) -> None:
    """Commit ROADMAP.md so the DONE mark survives forced checkouts.

    Retries briefly on index.lock contention (parallel workers). If every
    attempt fails the mark is still on disk — but the next _branch_start
    will wipe it, so a persistent failure is printed loudly.
    """
    cwd = os.getcwd()
    if not os.path.exists(os.path.join(cwd, ".git")):
        return
    msg = f"roadmap: mark done — {task_text(task_line)[:60]}"
    for i in range(attempts):
        _git(["add", "ROADMAP.md"], cwd)
        r = _git(["commit", "-m", msg, "--", "ROADMAP.md"], cwd)
        if r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr):
            return
        time.sleep(0.5 * (i + 1))
    print(f"  {RED}⚠ could not commit ROADMAP.md mark — it WILL be lost at the "
          f"next forced checkout: {(r.stdout + r.stderr).strip()[:200]}{RESET}")


# NOTE: task_graph.json is NOT regenerated at run time. work.py drives off
# ROADMAP.md via parse_all_tasks(); task_graph.json is the durable DAG record
# maintained by make_graph.py / plan_week.py only. A prior auto-sync helper
# lived here and was removed 2026-07-17 — regenerating the graph mid-run
# clobbered completed-phase task state and desynced it from ROADMAP.


# ─── File discovery ───────────────────────────────────────────────────────────

def all_source_files(project_root: str) -> list[str]:
    """Return all dart/py/js/go source files relative to project root.

    2026-07-13: added ".go" — its absence meant find_relevant_files returned
    [] for Go projects, so every worker coded with ZERO context and
    hallucinated field names, math helpers, and imports.
    """
    exts = {".dart", ".py", ".js", ".ts", ".go"}
    skip = {"build", ".dart_tool", ".git", "node_modules", ".fvm", ".venv", "logs"}
    result = []
    for p in Path(project_root).rglob("*"):
        if p.suffix in exts and not any(s in p.parts for s in skip):
            result.append(str(p.relative_to(project_root)))
    return sorted(result)


def _heuristic_files(task: str, files: list[str]) -> list[str]:
    """Keyword-based fallback when the planner model is unavailable."""
    keywords = re.findall(r"[A-Za-z][a-z]+(?:[A-Z][a-z]+)*|[a-z_]{4,}", task)
    scored: dict[str, int] = {}
    for f in files:
        score = sum(1 for kw in keywords if kw.lower() in f.lower())
        if score > 0:
            scored[f] = score
    # Always include game_core and the most relevant files
    top = sorted(scored, key=lambda f: scored[f], reverse=True)[:8]
    extras = [f for f in files if "game_core" in f or "astro_game" in f]
    return list(dict.fromkeys(top + extras))[:10]  # deduplicate, max 10


def find_relevant_files(task: str, project_root: str) -> list[str]:
    """Use Ollama 4B to pick which files are relevant. Falls back to heuristics."""
    files = all_source_files(project_root)
    if not files:
        return []

    try:
        raw = ollama(
            PLANNER_MODEL,
            (
                "You select source files relevant to a coding task. "
                "Return ONLY a JSON array of file paths chosen from the provided list. "
                "Include files that will change AND files needed as context (imports, base classes). "
                "Maximum 10 files. No explanation outside the JSON array."
            ),
            f"Task: {task}\n\nAvailable files:\n" + "\n".join(files),
            timeout=300,
        )
        start, end = raw.find("["), raw.rfind("]") + 1
        chosen = json.loads(raw[start:end])
        result = [f for f in chosen if os.path.exists(os.path.join(project_root, f))]
        if result:
            return result
    except Exception as e:
        print(f"  {DIM}(4B planner unavailable: {e} — using keyword fallback){RESET}")

    return _heuristic_files(task, files)


# ─── Code execution ───────────────────────────────────────────────────────────

def read_files(paths: list[str], project_root: str, max_chars: int = 10000) -> dict[str, str]:
    # 2026-07-13: max_chars 3000 → 10000. At 3000, sim/API.md (7.4 KB) and
    # spec/03-simulation.md (8 KB) were cut mid-file, so every phase-p2+ task
    # ran without its function contract and hallucinated fields/signatures.
    # All tiers now run 32k+ ctx — worst case ~9 files × 10 KB ≈ 25k chars
    # ≈ 8k tokens, well inside the pre-flight check in ollama().
    result = {}
    for rel in paths:
        full = os.path.join(project_root, rel)
        if os.path.exists(full):
            content = open(full).read()
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
            result[rel] = content
    return result


# ─── Project-agnostic locked-files registry ───────────────────────────────────
# Locked files are loaded from .sovereign_config.json in each project root.
# This object supports both exact paths ("lib/main.dart") and directory
# prefixes ("android/", "ios/") so call sites (using `in LOCKED_FILES`) need
# no changes — prefix matching is baked into __contains__.

class _LockedSet:
    """Set-like container that matches exact paths AND configured prefixes."""
    def __init__(self) -> None:
        self._exact:    set[str]  = set()
        self._prefixes: list[str] = []

    # ── Mutation ──────────────────────────────────────────────────────────────
    def add(self, path: str) -> None:
        self._exact.add(path)

    def add_prefix(self, prefix: str) -> None:
        """Register a directory prefix: any path starting with it is locked."""
        if not prefix.endswith("/"):
            prefix += "/"
        self._prefixes.append(prefix)

    def update(self, paths) -> None:
        for p in paths:
            self._exact.add(p)

    def clear(self) -> None:
        self._exact.clear()
        self._prefixes.clear()

    # ── Query ─────────────────────────────────────────────────────────────────
    def __contains__(self, item: object) -> bool:
        s = str(item)
        return s in self._exact or any(s.startswith(p) for p in self._prefixes)

    def __iter__(self):
        return iter(self._exact)

    def __len__(self) -> int:
        return len(self._exact)

    def __bool__(self) -> bool:
        return bool(self._exact) or bool(self._prefixes)

    def __repr__(self) -> str:
        return (f"_LockedSet(exact={len(self._exact)}, "
                f"prefixes={self._prefixes})")


LOCKED_FILES: _LockedSet = _LockedSet()

# ─── Known bad patterns ───────────────────────────────────────────────────────
# Checked against every proposed file before it is written to disk.
# If matched, the file is rejected and the hint is appended to errors
# so the 35B gets a precise, actionable correction on the next attempt.
import re as _re
# ─── Bad patterns & error hints ────────────────────────────────────────────────
# Extracted to hints.py — import from there so each can be edited independently.
import hints  # noqa: E402  — pack loader; see hints.py CONTRACT
from hints import BAD_PATTERNS, ERROR_HINTS  # noqa: E402  (mutated in place)
import grounding  # noqa: E402  — identifier whitelist gate for Go projects
import grounders  # noqa: E402  — per-language grounding gate registry
import prompt_artifacts  # noqa: E402  — grounding gate for model-written prompt text


def apply_error_hints(error_output: str) -> str:
    """Prepend targeted hints for any recognised error patterns."""
    hints = []
    for pattern, hint in ERROR_HINTS:
        # re.search/re.match cannot take a flags argument if the pattern is already compiled.
        if isinstance(pattern, _re.Pattern):
            if pattern.search(error_output):
                hints.append(hint)
        else:
            if _re.search(pattern, error_output, _re.IGNORECASE):
                hints.append(hint)
    if hints:
        return "\n".join(hints) + "\n\n" + error_output
    return error_output


def check_bad_patterns(rel_path: str, content) -> list[str]:
    """Return list of violation messages found in content.

    Accepts str or list[str] (models sometimes return file lines as a JSON array).
    """
    if isinstance(content, list):
        content = "\n".join(str(line) for line in content)
    elif not isinstance(content, str):
        content = str(content)
    violations = []
    for pattern, msg in BAD_PATTERNS:
        if isinstance(pattern, _re.Pattern):
            if pattern.search(content):
                violations.append(f"[{rel_path}] {msg}")
        else:
            if _re.search(pattern, content):
                violations.append(f"[{rel_path}] {msg}")
    return violations


def _extract_dart_api_stub(content: str, max_chars: int = 800) -> str:
    """Extract a compact read-only API stub from a Dart source file.

    Returns class declarations, constructor signatures, and public field/method
    signatures — enough for the model to use the API correctly without seeing
    the full implementation.  Private members (_name) are omitted.
    """
    import re as _re
    lines = content.splitlines()
    stub_lines: list[str] = []
    in_class = False
    brace_depth = 0

    for line in lines:
        stripped = line.strip()

        # Skip private members
        if _re.match(r'(final|late|var|void|Future|List|Map|Set|int|double|bool|String)\s+_', stripped):
            continue
        if _re.match(r'_\w+\(', stripped):
            continue

        # Class declaration
        if _re.match(r'(abstract\s+)?class\s+\w+', stripped):
            in_class = True
            brace_depth = 0
            stub_lines.append(line.rstrip())
            continue

        if in_class:
            brace_depth += stripped.count('{') - stripped.count('}')
            if brace_depth < 0:
                stub_lines.append('}')
                in_class = False
                brace_depth = 0
                continue

            # Only emit top-level members (brace_depth == 1 means we're directly inside the class)
            if brace_depth != 1:
                continue
            # Skip comments, blank lines, control-flow keywords
            if not stripped or stripped.startswith('//') or stripped.startswith('/*'):
                continue
            if _re.match(r'(if|else|for|while|switch|return|throw|assert|case|default|break|continue)\b', stripped):
                continue

            # Constructor: ClassName( — top-level, no return type
            if _re.match(r'[A-Z]\w*[\._]?\w*\s*\(', stripped):
                stub_lines.append('  ' + stripped.split('{')[0].split('=>')[0].strip() + ';')
            # Public field declaration
            elif _re.match(r'(final\s+|late\s+|static\s+const\s+|static\s+final\s+|static\s+)?(int|double|bool|String|var|dynamic|[A-Z]\w*)[?<\s]', stripped):
                decl = stripped.split('=')[0].strip().rstrip(';') + ';'
                if not decl.startswith('_'):
                    stub_lines.append('  ' + decl)
            # Public method / getter signature (return type + name)
            elif _re.match(r'(static\s+|override\s+)?(void|Future|bool|int|double|String|List|Map|Set|[A-Z]\w*)[?<\s].*[a-z]\w*\s*[\(\{]', stripped):
                sig = stripped.split('{')[0].split('=>')[0].strip()
                if not _re.match(r'_', sig.split()[-1] if ' ' in sig else sig):
                    stub_lines.append('  ' + sig + ' { ... }')
            # enum
            elif _re.match(r'enum\s+\w+', stripped):
                stub_lines.append(line.rstrip())

    result = '\n'.join(stub_lines)
    return result[:max_chars] if len(result) > max_chars else result


def _ts_fn_sig(lines: list[str], i: int) -> tuple[str, int]:
    """Signature (body dropped) for an exported function starting at line i.

    Matches the parameter parens across lines and cuts at the body brace, so
    inline object-typed params (e.g. `isIdle(u: { target: Vec2 | null })`) and
    multi-line signatures survive intact. Returns (signature, next_index).
    """
    buf = lines[i]
    depth = buf.count('(') - buf.count(')')
    j = i
    while depth > 0 and j + 1 < len(lines):
        j += 1
        buf += ' ' + lines[j].strip()
        depth += lines[j].count('(') - lines[j].count(')')
    p = buf.find('(')
    close, d = -1, 0
    for idx in range(p, len(buf)):
        if buf[idx] == '(':
            d += 1
        elif buf[idx] == ')':
            d -= 1
            if d == 0:
                close = idx
                break
    after = buf[close + 1:] if close != -1 else ''
    b = after.find('{')
    ret = after[:b] if b != -1 else after
    sig = (buf[:close + 1] if close != -1 else buf) + ret
    return sig.strip() + ' { ... }', j + 1


def _extract_ts_api_stub(content: str, max_chars: int = 1500) -> str:
    """Read-only API surface of a TypeScript/JavaScript file.

    Keeps exported function/class signatures and FULL interface / object-type /
    enum bodies (their fields ARE the API the model must use) plus full
    const/let/type-alias lines (constant VALUES like MOTE_SPEED = 60 matter).
    Function bodies are dropped to save budget.
    """
    import re as _re
    lines = content.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        # Exported interface / enum / object-type alias: keep the whole brace block.
        if (_re.match(r'export\s+(interface|enum)\b', s)
                or _re.match(r'export\s+type\s+\w+[^=]*=\s*\{', s)):
            block = [lines[i].rstrip()]
            depth = s.count('{') - s.count('}')
            i += 1
            while i < n and depth > 0:
                block.append(lines[i].rstrip())
                depth += lines[i].count('{') - lines[i].count('}')
                i += 1
            out.append("\n".join(block))
            continue
        # Exported function: signature only.
        if _re.match(r'export\s+(async\s+)?function\b', s):
            sig, i = _ts_fn_sig(lines, i)
            out.append(sig)
            continue
        # Exported class: declaration head only.
        if _re.match(r'export\s+(abstract\s+)?class\b', s):
            out.append(s.split('{')[0].strip() + ' { ... }')
            i += 1
            continue
        # Exported const / let / one-line type alias: keep the full line.
        if _re.match(r'export\s+(const|let|type)\b', s):
            out.append(s.rstrip())
            i += 1
            continue
        i += 1
    return "\n".join(out).strip()[:max_chars]


def _extract_api_stub(path: str, content: str, max_chars: int = 1500) -> str:
    """Language-aware read-only API stub for a locked file, dispatched by extension.

    2026-07-16: locked files were run unconditionally through the Dart
    extractor, so on TypeScript projects (GalaxicanJS) vec.ts / types.ts
    produced an EMPTY stub — the model never saw `dist`, `pos: Vec2`, or
    `interface Mote`, and hallucinated `vecDist`, `.position`, `sortedIds`
    on nearly every task, escalating both A/B arms for the same reason.
    Markdown (API.md) is injected whole — never code-parsed.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".md":
        return content[:max_chars]
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
        return _extract_ts_api_stub(content, max_chars=max_chars)
    return _extract_dart_api_stub(content, max_chars=min(max_chars, 800))


# Cache of (symbol->module, module->exports) per project_root. Cheap to build
# (regex over the source tree) but rebuilt only when explicitly invalidated;
# exports change rarely between tasks and a slightly stale map is still correct
# for the vast majority of symbols.
_SYMBOL_INDEX_CACHE: dict[str, tuple[dict, dict, dict]] = {}


def _symbol_index(project_root: str, refresh: bool = False):
    """Memoized (symbol->module, module->exports, funcname->signature)."""
    key = os.path.abspath(project_root)
    if refresh or key not in _SYMBOL_INDEX_CACHE:
        try:
            idx, exp = import_fixer.build_symbol_index(project_root)
            sigs = import_fixer.build_signature_index(project_root)
            _SYMBOL_INDEX_CACHE[key] = (idx, exp, sigs)
        except Exception:
            _SYMBOL_INDEX_CACHE[key] = ({}, {}, {})
    return _SYMBOL_INDEX_CACHE[key]


def _target_ts_file(task: str) -> str | None:
    """Extract the target .ts/.tsx path a task names: 'In src/sim/foo.ts: ...'."""
    m = re.search(r'\bIn ([\w./-]+\.tsx?):', task)
    return m.group(1) if m else None


def _module_exists(base_no_ext: str, project_root: str) -> bool:
    """True if a project module resolves to a real file (.ts/.tsx/.json/index)."""
    for cand in (base_no_ext + ".ts", base_no_ext + ".tsx", base_no_ext + ".json",
                 os.path.join(base_no_ext, "index.ts"),
                 os.path.join(base_no_ext, "index.tsx")):
        if os.path.exists(os.path.join(project_root, cand)):
            return True
    return False


def _missing_local_deps(task: str, project_root: str) -> list[str]:
    """Project source files a task says it depends on that do NOT exist yet.

    A composition/integration task that imports a sibling module which hasn't
    landed can never pass — the worker just hallucinates the missing symbol.
    Rather than burn every attempt, the caller defers such a task to the retry
    pass (returns "skipped") so its dependency has a chance to land first.

    Only counts EXPLICIT project paths written in the task text ("src/render/
    useGameLoop", "src/sim/lasso.ts") and quoted relative imports ("../sim/x").
    Never flags the task's own target file, node_modules, or framework pkgs, so
    a task with no named-but-missing dependency is never falsely deferred.
    """
    target = _target_ts_file(task)
    target_dir = os.path.dirname(target) if target else ""
    target_no_ext = re.sub(r'\.tsx?$', '', target) if target else None

    candidates: set[str] = set()

    # Absolute-style project references: src/render/useGameLoop(.ts)?
    for m in re.findall(r'\bsrc/[\w./-]+', task):
        p = m.rstrip('.,;)')
        if p.endswith((".md",)):        # spec docs handled elsewhere
            continue
        candidates.add(re.sub(r'\.tsx?$', '', p))

    # Quoted relative imports resolved against the target's directory.
    if target_dir:
        for rel in re.findall(r"""['"](\.\.?/[\w./-]+)['"]""", task):
            resolved = os.path.normpath(os.path.join(target_dir, rel))
            candidates.add(re.sub(r'\.tsx?$', '', resolved))

    missing = []
    for c in sorted(candidates):
        if target_no_ext and c == target_no_ext:
            continue                    # never flag the file we're writing
        if not c.startswith("src/"):
            continue                    # only project sources
        if os.path.isdir(os.path.join(project_root, c)):
            continue                    # a directory mention, not a module import
        if not _module_exists(c, project_root):
            missing.append(c)
    return missing


def implement_task(task: str, file_contents: dict[str, str], errors: str = "",
                   model: str | None = None,
                   is_test: bool = False,
                   project_root: str = ".") -> tuple[dict[str, str], dict]:
    """Ask Ollama to implement the task. Returns {rel_path: new_content}."""
    model = model or EXECUTOR_MODEL
    # Split context: editable files get full content; locked files get a compact
    # read-only API stub so the model knows what classes/constructors exist without
    # being able to overwrite them.  Previously locked files were stripped entirely,
    # which caused the model to hallucinate APIs from training data.
    editable = {p: c for p, c in file_contents.items() if p not in LOCKED_FILES}
    locked_context = {p: c for p, c in file_contents.items() if p in LOCKED_FILES}

    context = "\n\n".join(
        f"=== {path} ===\n{content}"
        for path, content in editable.items()
    )

    # Inject compact stubs for locked files — class name, constructor, public API only.
    # Capped at 800 chars per file to keep tier-1 token budget manageable.
    if locked_context:
        stub_parts = []
        for path, content in locked_context.items():
            stub = _extract_api_stub(path, content, max_chars=1500)
            if stub:
                stub_parts.append(f"=== {path} [READ-ONLY — do not return this path] ===\n{stub}")
        if stub_parts:
            context += "\n\n// ── Locked file APIs (read-only context) ──\n" + "\n\n".join(stub_parts)
    vision = open("VISION.md").read() if os.path.exists("VISION.md") else ""
    _project_rules = open(".roorules").read() if os.path.exists(".roorules") else ""
    _global_rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global_rules.md")
    _global_rules = open(_global_rules_path).read() if os.path.exists(_global_rules_path) else ""
    rules = (_global_rules + "\n\n---\n\n# Project-specific rules\n\n" + _project_rules).strip()

    # Inject API guide so the model knows what already exists before inventing new classes.
    # Skip for tier-1 (7B): the guide alone costs ~5 000 tokens and leaves too little room
    # for file context. Tier-2 and Tier-3 (MoE models) get a trimmed guide to save headroom.
    # Claude (called separately via _escalate_to_claude) always receives the full guide.
    _api_guide_path = os.path.join(project_root, "docs", "API_GUIDE.md")
    api_guide_block = ""
    _is_tier1 = model == TIER1_MODEL
    _is_moe_tier = model in (TIER2_MODEL, TIER3_MODEL)  # T2/T3 are MoE — trim guide to save headroom
    # T4 (32B dense) and Claude get the full guide; T1 gets none (too costly for 7B context)
    if os.path.exists(_api_guide_path) and not _is_tier1:
        _guide = open(_api_guide_path).read()
        _guide = _guide[:6000] if _is_moe_tier else _guide
        api_guide_block = f"\n\nEXISTING API REFERENCE — read before creating any new class or file:\n{_guide}"

    # Inject top recurring error patterns as a "known pitfalls" block
    pitfalls_block = ""
    try:
        adv = _get_advisor()
        top = adv.top_error_patterns(project_root, n=8)
        if top:
            lines = "\n".join(f"  - {p['code']} ({p['count']}×)" for p in top)
            pitfalls_block = (
                f"\n\nKNOWN PITFALLS (most frequent errors in this project — avoid these):\n"
                f"{lines}"
            )
    except Exception:
        pass

    # Compact locked list — show directories grouped to save tokens
    locked_dirs: dict[str, list[str]] = {}
    for f in sorted(LOCKED_FILES):
        d = f.rsplit("/", 1)[0] if "/" in f else "root"
        locked_dirs.setdefault(d, []).append(f.rsplit("/", 1)[-1])
    locked_list = "\n".join(
        f"  {d}/: {', '.join(names)}" for d, names in sorted(locked_dirs.items())
    )
    error_block = f"\n\nPrevious attempt failed with these errors — fix them:\n{errors}" if errors else ""

    # Test tasks get an extra hard constraint injected into the system prompt
    # so the model understands scope BEFORE it generates any code.
    scope_block = (
        "\n\nTASK SCOPE — CRITICAL:\n"
        "This is a TEST-ONLY task. You may ONLY create or modify files under test/.\n"
        "NEVER write to any file under lib/ — not even to add a missing feature.\n"
        "If the feature you need to test does not exist or has the wrong API:\n"
        "  • Write a skip-marked placeholder test and stop:\n"
        "      test('...', () {}, skip: 'feature not implemented — implement task required first');\n"
        "  • Do NOT invent or add the implementation yourself.\n"
        "  • Do NOT modify source files to make your test compile.\n"
        "The implement task will follow — your job here is ONLY the test file.\n"
        if is_test else ""
    )

    system_prompt = (
        # Language-aware role line (2026-07-13): this said "Dart/Flutter coding
        # agent" unconditionally — on Go projects the model was primed with the
        # wrong language, wrong example paths, and Dart idioms.
        (f"You are a Go coding agent. Implement the given task.\n"
           if PROJECT_LANGUAGE == "go" else
           "You are a Dart/Flutter coding agent. Implement the given task.\n")
        + "Return ONLY a JSON object where:\n"
        + ("  - Keys are file paths exactly as given (e.g. 'sim/mote_move.go')\n"
           if PROJECT_LANGUAGE == "go" else
           "  - Keys are file paths exactly as given (e.g. 'lib/game/game_core.dart')\n")
        + "  - Values are the COMPLETE new file contents (not diffs, not snippets)\n"
        "Only include files that DIRECTLY change to implement this specific task.\n"
        "DO NOT touch files that are not required. DO NOT refactor or improve unrelated files.\n"
        "DO NOT create new files unless the task explicitly says to create one.\n"
        + ("This task changes EXACTLY ONE file — the one named in the task. Return exactly one key.\n"
           if PROJECT_LANGUAGE == "go" else
           "Typical task requires 1-3 files. If you find yourself changing more than 5, stop and reconsider.\n")
        + "Keep implementations MINIMAL — write the fewest lines that make the task work. "
        "Do not add elaborate systems, helpers, or abstractions unless required.\n"
        + ("COMPLETENESS: the file must be complete and compilable — every opened brace closed, "
           "no trailing truncation. Before finishing, verify the final character of the file "
           "content is the closing brace of the last function.\n"
           "NESTING: keep nesting depth <= 3. Use early `continue`/`return` guard clauses "
           "instead of nested if-blocks — deep nesting causes brace-count errors.\n"
           if PROJECT_LANGUAGE == "go" else "")
        + "No explanation or markdown outside the JSON object.\n\n"
        "CRITICAL — these files are LOCKED and will be silently ignored if returned:\n"
        f"{locked_list}\n\n"
        "Coding rules:\n"
        f"{rules}"
        f"{pitfalls_block}"
        f"{scope_block}"
    )
    # Import map (TS projects): tell the model EXACTLY which module exports each
    # symbol, with the relative specifier already computed for the target file.
    # This is the single biggest curb on the "invented import / wrong module"
    # loop — the dominant local-model failure. Harmless no-op for non-TS.
    import_map_block = ""
    _tgt = _target_ts_file(task)
    if _tgt:
        try:
            idx, exp, sigs = _symbol_index(project_root)
            _map = import_fixer.build_import_map(project_root, _tgt, idx, exp, sigs)
            if _map:
                import_map_block = f"\n\n{_map}\n"
        except Exception:
            import_map_block = ""

    user_prompt = (
        f"Task: {task}\n\n"
        f"Project vision summary:\n{vision[:1500]}\n"
        f"{api_guide_block}"
        f"{import_map_block}\n"
        f"Current file contents:\n{context}"
        f"{error_block}"
    )
    # Scale timeout with model size: large 32B models need up to 30 min on a
    # memory-constrained Mac (macOS must evict cached files + swap before the
    # model fits in unified memory — this can take 20+ min when RAM is tight).
    # (3-4 min to load + 10-12 min to generate a full file).
    size_gb = _model_size_gb(model)
    coding_timeout = 1800 if size_gb >= _LARGE_MODEL_THRESHOLD_GB else 600
    raw = ollama(model, system_prompt, user_prompt, timeout=coding_timeout)
    call_info = {"system": system_prompt, "user": user_prompt, "raw": raw}

    start, end = raw.find("{"), raw.rfind("}") + 1
    if start == -1:
        return {}, call_info
    try:
        return json.loads(raw[start:end]), call_info
    except Exception:
        return {}, call_info


def write_changes(changes: dict[str, str], project_root: str,
                  test_only: bool = False,
                  required_file: str | None = None) -> tuple[list[str], str]:
    """Write changes, skipping locked files and bad-pattern violations.

    When test_only=True (a 'Write unit test' task), any file outside test/ is
    rejected with a scope-violation message that is fed back to the model on
    the next attempt — prompting it to write a skip-marked placeholder instead
    of inventing the implementation.

    Returns (written_paths, pattern_error_string).
    """
    written = []
    pattern_errors: list[str] = []
    for rel_path, content in changes.items():
        # Target-file enforcement (2026-07-15): Go tasks name exactly one
        # file ("In game/tick.go: ..."). The model sometimes writes a
        # DIFFERENT path (observed: sim/tick.go instead of game/tick.go,
        # every attempt, all tiers) and nothing stopped it — the wrong file
        # then fails vet forever. Reject non-matching paths outright.
        if required_file and rel_path != required_file:
            msg = (
                f"WRONG FILE [{rel_path}]: this task implements {required_file} "
                f"and NOTHING else. You returned '{rel_path}'. Return exactly one "
                f"key: '{required_file}', with the package declaration matching "
                f"its directory."
            )
            print(f"  {RED}(blocked wrong-file write: {rel_path} ≠ {required_file}){RESET}")
            pattern_errors.append(msg)
            continue
        if rel_path in LOCKED_FILES:
            print(f"  {DIM}(skipped locked file: {rel_path}){RESET}")
            continue
        # Frozen-suite guard (Go projects): the acceptance tests are authored
        # once and locked (conformance/); workers must NEVER write *_test.go,
        # anywhere — a worker-written test could game its own acceptance gate.
        if PROJECT_LANGUAGE == "go" and rel_path.endswith("_test.go"):
            msg = (
                f"SCOPE VIOLATION [{rel_path}]: this project's test suite is frozen "
                f"(conformance/ is locked). Never write *_test.go files. Implement "
                f"the feature in the exact file named by the task; acceptance is the "
                f"existing conformance suite."
            )
            print(f"  {RED}(blocked frozen-suite violation: {rel_path}){RESET}")
            pattern_errors.append(msg)
            continue
        # Scope guard: test tasks must only write test files.
        # Allowed: test/**  OR  lib/**/*_test.dart (Flame projects keep tests in lib/)
        _is_test_file = (
            rel_path.startswith("test/")
            or (rel_path.startswith("lib/") and rel_path.endswith("_test.dart"))
        )
        if test_only and not _is_test_file:
            msg = (
                f"SCOPE VIOLATION [{rel_path}]: This is a 'Write unit test' task — "
                f"only test files are permitted (test/**  or  lib/**/*_test.dart). "
                f"'{rel_path}' is a source file and must NOT be modified here. "
                f"If the feature under test is missing, write a skip-marked placeholder:\n"
                f"  test('...', () {{}}, skip: 'feature not implemented — implement task required first');"
            )
            print(f"  {RED}(blocked scope violation: {rel_path}){RESET}")
            pattern_errors.append(msg)
            continue
        # Guard: model occasionally returns a nested dict or a list of lines
        # instead of a plain string.  Coerce gracefully before any pattern checks.
        if not isinstance(content, str):
            if isinstance(content, list):
                # Model returned lines as a JSON array — join them back.
                content = "\n".join(str(line) for line in content)
            elif isinstance(content, dict):
                # Autofix sometimes returns {'imports': [...], 'fixes': [...]} —
                # this is a patch descriptor, NOT file content.  Writing it as-is
                # would corrupt the file with a Python dict string.  Reject it.
                if "imports" in content or "fixes" in content:
                    msg = (
                        f"INVALID FORMAT [{rel_path}]: model returned a patch descriptor "
                        f"dict (with 'imports'/'fixes' keys) instead of complete file content. "
                        f"You MUST return the full file as a plain string under the file path key. "
                        f"Do not return patch objects — return the entire corrected file content."
                    )
                    print(f"  {RED}(blocked patch-dict output: {rel_path}){RESET}")
                    pattern_errors.append(msg)
                    continue
                # Other dicts: try common content keys, otherwise reject.
                extracted = content.get("content") or content.get("code")
                if not extracted:
                    msg = (
                        f"INVALID FORMAT [{rel_path}]: model returned an unrecognised dict "
                        f"instead of file content (keys: {list(content.keys())}). "
                        f"Return the full file content as a plain string."
                    )
                    print(f"  {RED}(blocked unrecognised dict output: {rel_path}){RESET}")
                    pattern_errors.append(msg)
                    continue
                content = extracted
            else:
                content = str(content)
        # Only check Dart source files for bad patterns — not docs, XML, YAML, etc.
        import pathlib
        violations = check_bad_patterns(rel_path, content) if pathlib.Path(rel_path).suffix in ('.dart', '.go', '.ts', '.tsx', '.py', '.swift') else []
        # Grounding gate: reject files that reference identifiers or import
        # paths that exist nowhere in the project, its dependencies, or the
        # change-set itself. Catches hallucinated APIs (g.FindSquad, .IsIdle,
        # package:your_app_name/...) instantly, before the compiler or analyzer
        # ever runs, and the rejection message (with did-you-mean suggestions)
        # is fed back to the model on the next attempt — rejection IS the
        # repair prompt.
        #
        # Was hardcoded to Go until 2026-07-25. On the Dart project that left
        # 73% of failing attempts (invented imports and symbols) entirely
        # ungated. Language selection now lives in grounders.py; adding a
        # language does not mean editing this function.
        _grounder = grounders.for_language(PROJECT_LANGUAGE)
        if not violations and _grounder.handles(rel_path):
            # Deterministic repairs BEFORE the check: Go models reliably drop
            # 1-2 closing braces on deeply nested functions ("unexpected EOF,
            # expected }") — fix rather than burn a whole attempt.
            content, _note = _grounder.repair(content)
            if _note:
                print(f"  {DIM}({_note}: {rel_path}){RESET}")
            # Sibling files in the same change set are not on disk yet, but
            # their declarations and paths are legitimate references.
            _extra_decl: set[str] = set()
            _extra_files: set[str] = set()
            for _other, _oc in changes.items():
                if _other == rel_path or not isinstance(_oc, str):
                    continue
                if _grounder.handles(_other):
                    _extra_decl |= _grounder.declared_names(_oc)
                _extra_files.add(_other)
            violations = _grounder.check(
                rel_path, content, project_root,
                extra_declared=_extra_decl, extra_files=_extra_files)
        if violations:
            for v in violations:
                print(f"  {RED}(blocked bad pattern: {v[:80]}){RESET}")
            pattern_errors.extend(violations)
            continue  # don't write this file
        full = os.path.join(project_root, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        written.append(rel_path)
    return written, "\n".join(pattern_errors)


def update_api_guide(written_paths: list[str], changes: dict[str, str],
                     project_root: str) -> None:
    """After a task passes, extract new public APIs and append them to API_GUIDE.md.

    Only processes lib/ Dart source files (not tests, not locked files).
    Uses the T1 7B model — fast and cheap.  Skips files whose top-level class
    name already appears in the guide.  Appends at most one entry per file.
    """
    guide_path = os.path.join(project_root, "docs", "API_GUIDE.md")
    if not os.path.exists(guide_path):
        return

    with open(guide_path) as f:
        existing_guide = f.read()

    candidates = [
        p for p in written_paths
        if p.startswith("lib/") and p.endswith(".dart") and p not in LOCKED_FILES
    ]
    if not candidates:
        return

    new_entries: list[str] = []
    for rel_path in candidates:
        content = changes.get(rel_path, "")
        if not content:
            full = os.path.join(project_root, rel_path)
            if os.path.exists(full):
                with open(full) as f:
                    content = f.read()
        if not content:
            continue

        # Skip if any public class/provider name from this file is already in the guide.
        # A rough heuristic: if the filename stem already appears as a code word in the guide.
        stem = rel_path.rsplit("/", 1)[-1].replace(".dart", "")
        # Convert snake_case to CamelCase and check both forms
        camel = "".join(w.title() for w in stem.split("_"))
        if stem in existing_guide or camel in existing_guide:
            continue

        try:
            entry = ollama(
                FINDER_MODEL,
                (
                    "You are a Dart API documentation extractor.\n"
                    "Given a Dart source file, output a COMPACT API reference entry "
                    "suitable for appending to a developer guide.\n"
                    "Include ONLY: class/enum name, constructor signature with all "
                    "parameters, public method signatures, public getters, and any "
                    "top-level provider variable names.\n"
                    "Format as a markdown ### heading followed by a dart code block "
                    "with signatures only (no bodies).\n"
                    "If the file contains no significant new public API "
                    "(e.g. it is a private helper or test), output exactly: SKIP\n"
                    "Keep the entry under 40 lines."
                ),
                f"File: {rel_path}\n\n```dart\n{content[:3000]}\n```",
                timeout=120,
            )
        except Exception:
            continue

        entry = entry.strip()
        if not entry or entry.upper().startswith("SKIP"):
            continue

        new_entries.append(
            f"\n\n### Auto-documented: `{rel_path}`\n"
            f"<!-- auto-generated on task success — edit manually if wrong -->\n"
            f"{entry}"
        )

    if not new_entries:
        return

    # Append under a persistent section header (created once if not present)
    separator = "\n\n---\n\n## Auto-documented APIs\n"
    with open(guide_path, "a") as f:
        if "## Auto-documented APIs" not in existing_guide:
            f.write(separator)
        for entry in new_entries:
            f.write(entry)

    print(f"  {DIM}📖 API guide updated ({len(new_entries)} new entries){RESET}")


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a git command quietly. Never raises — caller checks returncode."""
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _branch_start(project_root: str, task_idx: int, task: str) -> str | None:
    """Create an isolated branch for this task. Returns branch name or None if git unavailable."""
    if not GIT_BRANCHES:
        return None
    if not os.path.exists(os.path.join(project_root, ".git")):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", task[:45].lower()).strip("-")
    branch = f"task-{task_idx}-{slug}"
    # Always start from main so branches don't stack on each other.
    # MUST be a forced checkout: write_changes() writes the model's output
    # straight to disk before validation ever runs, so the working tree is
    # essentially always dirty by the time we get here. A plain `checkout
    # main` silently no-ops on a dirty tree instead of erroring — it does
    # NOT restore main's clean file contents — so a previous task's failed
    # (never-committed) writes were surviving in the working tree and
    # contaminating every task after it, even though the branches themselves
    # forked cleanly from main at the git-ref level. Found 2026-07-13: task
    # 4's abandoned squad_move.go (with a hallucinated import of a package
    # that doesn't exist) kept failing go vet for tasks 8 and 10, neither of
    # which had anything to do with squad_move.go.
    _git(["checkout", "-f", MAIN_BRANCH], project_root)
    # -e logs: never clean the logs/ dir — tier2_queue.jsonl, velocity.jsonl,
    # the failure ledger etc. live there untracked; a bare `clean -fd` was
    # deleting the --deep queue between tasks (2026-07-13).
    _git(["clean", "-fd", "-e", "logs"], project_root)
    r = _git(["checkout", "-b", branch], project_root)
    if r.returncode != 0:
        # Branch exists from a previous aborted run — reuse it
        _git(["checkout", "-f", branch], project_root)
    return branch


def _branch_merge(project_root: str, branch: str, task_idx: int, task: str) -> None:
    """Commit the task changes, merge to the integration branch, delete the branch."""
    _git(["add", "-A"], project_root)
    _git(["commit", "-m", f"task {task_idx}: {task[:72]}"], project_root)
    _git(["checkout", "-f", MAIN_BRANCH], project_root)
    _git(["merge", "--no-ff", branch, "-m", f"Merge task-{task_idx}"], project_root)
    _git(["branch", "-D", branch], project_root)


def _branch_abandon(project_root: str, branch: str) -> None:
    """Discard a failed task branch and return to main.

    Force + clean: this branch failed, so nothing it wrote — committed or
    not — should be allowed to survive into main or leak into the next
    task's working tree. See the note in _branch_start for why a plain
    (non-forced) checkout isn't enough here.
    """
    _git(["checkout", "-f", MAIN_BRANCH], project_root)
    _git(["clean", "-fd", "-e", "logs"], project_root)
    _git(["branch", "-D", branch], project_root)


def _post_planning_commit(project_root: str,
                          files: list[str] | None = None) -> bool:
    """Commit and push sprint planning artifacts so cloud workers can pull them.

    Called after plan_week.py (or standup) generates ROADMAP.md + task_graph.json.
    Only commits if there are actual staged changes — safe to call repeatedly.
    Returns True if everything succeeded (or there was nothing to commit).
    """
    if not os.path.exists(os.path.join(project_root, ".git")):
        print(f"  {YELLOW}No .git found in {project_root} — skipping sprint commit{RESET}")
        return False

    stage_targets = files or ["ROADMAP.md", "task_graph.json", ".roorules", "docs/"]
    existing = [f for f in stage_targets
                if os.path.exists(os.path.join(project_root, f))]
    if not existing:
        print(f"  {YELLOW}Nothing to commit (no planning files found){RESET}")
        return False

    _git(["add"] + existing, project_root)

    # Bail out cleanly if nothing actually changed
    staged = _git(["diff", "--cached", "--quiet"], project_root)
    if staged.returncode == 0:
        print(f"  {DIM}Sprint planning files unchanged — no commit needed{RESET}")
        return True

    today = date.today().isoformat()
    commit_r = _git(["commit", "-m", f"sprint planning: {today}"], project_root)
    if commit_r.returncode != 0:
        print(f"  {YELLOW}git commit failed: {commit_r.stderr.strip()}{RESET}")
        return False

    print(f"  {GREEN}✓ Sprint plan committed ({', '.join(existing)}){RESET}")

    push_r = _git(["push", "origin", "HEAD"], project_root)
    if push_r.returncode != 0:
        print(f"  {YELLOW}git push failed (no remote / auth issue?):\n"
              f"    {push_r.stderr.strip()[:120]}{RESET}")
        return False

    print(f"  {GREEN}✓ Sprint plan pushed to origin — cloud workers can pull{RESET}")
    return True


# ─── Project config (.sovereign_config.json) ──────────────────────────────────

# Per-project validation gates (language-agnostic). When non-empty, validate()
# runs these shell commands (in order, cwd = project root, fail on nonzero
# exit) INSTEAD of the built-in flutter/pytest/npm chain, and
# snapshot_baseline_errors() skips its flutter-analyze baseline entirely.
# Populated from .sovereign_config.json "validate_commands": list[str].
PROJECT_VALIDATE_COMMANDS: list[str] = []

# Project language from .sovereign_config.json "language" (default "dart").
# Currently used to: extend bad-pattern checks to .go files, and block all
# *_test.go writes in Go projects (their conformance suite is frozen).
PROJECT_LANGUAGE: str = "dart"

# Project identifier from .sovereign_config.json "project" (falls back to the
# project directory's basename if the key is absent). Used to scope Firestore
# lesson push/pull per-project — see _firestore_push_lessons/_firestore_pull_lessons.
# Added 2026-07-13: before this, error_patterns/project_rules were pushed and
# pulled with NO project field at all, so every project sharing this Firestore
# project (FIRESTORE_PROJECT) drew from one global, unscoped pool. In practice
# this meant a brand-new Go project pulled 433 Dart/Flutter-specific rules from
# an older unrelated project on its very first run, silently re-contaminating
# any local .roorules cleanup on every session start.
PROJECT_ID: str = ""

# Files fed as context to EVERY task, from .sovereign_config.json
# "context_always_include". Added 2026-07-13: the key existed only as prose in
# the config's "notes" — nothing implemented it, so Go workers never saw
# types.go/vec.go/API.md and hallucinated struct fields and helpers.
PROJECT_ALWAYS_INCLUDE: list[str] = []


def _load_project_config(project_root: str) -> dict:
    """Load .sovereign_config.json from project_root and apply settings.

    Recognised keys
    ---------------
    locked_files : list[str]
        Exact relative paths that workers must never rewrite.
        e.g. ["pubspec.lock", "lib/firebase_options.dart"]

    locked_prefixes : list[str]
        Directory prefixes — any file whose relative path *starts with* one of
        these is treated as locked.  Trailing slash is optional.
        e.g. ["android/", "ios/", "assets/"]

    hint_packs : list[str]
        Which hint_packs/ modules to load. Omit for the language defaults
        (see hints.DEFAULT_PACKS). Available: dart_core, flutter_ui,
        dart_riverpod, dart_flame, galaxican.

    disable_bad_patterns : list[str]
        Substrings; any pack entry whose PATTERN contains one is dropped.
        The escape hatch that did not exist before 2026-07-25.

    additional_bad_patterns : list[{"pattern": str, "hint": str}]
        Extra entries appended to BAD_PATTERNS after the packs load.

    additional_error_hints : list[{"pattern": str, "hint": str}]
        Extra entries appended to ERROR_HINTS after the packs load.

    Returns the raw parsed dict (or {} if file absent / malformed).
    Mutates LOCKED_FILES and extends BAD_PATTERNS / ERROR_HINTS in-place.
    """
    cfg_path = os.path.join(project_root, ".sovereign_config.json")
    if not os.path.exists(cfg_path):
        # Silently ok — not every project needs a config file.
        return {}

    try:
        with open(cfg_path) as fh:
            cfg = json.load(fh)
    except Exception as exc:
        print(f"  {YELLOW}⚠  Could not parse .sovereign_config.json: {exc}{RESET}")
        return {}

    # ── Locked files ─────────────────────────────────────────────────────────
    exact = cfg.get("locked_files", [])
    if not isinstance(exact, list):
        print(f"  {YELLOW}⚠  .sovereign_config.json: 'locked_files' must be a list{RESET}")
        exact = []
    LOCKED_FILES.update(exact)

    prefixes = cfg.get("locked_prefixes", [])
    if not isinstance(prefixes, list):
        print(f"  {YELLOW}⚠  .sovereign_config.json: 'locked_prefixes' must be a list{RESET}")
        prefixes = []
    for p in prefixes:
        LOCKED_FILES.add_prefix(p)

    # ── Hint packs ────────────────────────────────────────────────────────────
    # Must run BEFORE additional_* below: load_packs() replaces the contents of
    # BAD_PATTERNS / ERROR_HINTS in place, so anything appended first would be
    # wiped. Reads `language` directly rather than PROJECT_LANGUAGE, which is
    # not assigned until further down this same function.
    _pack_lang = str(cfg.get("language", "") or "").lower() or None
    _packs = cfg.get("hint_packs")
    if _packs is not None and not (isinstance(_packs, list)
                                   and all(isinstance(p, str) for p in _packs)):
        print(f"  {YELLOW}⚠  .sovereign_config.json: 'hint_packs' must be a "
              f"list of strings — falling back to language defaults{RESET}")
        _packs = None
    _disable = cfg.get("disable_bad_patterns", [])
    if not isinstance(_disable, list):
        print(f"  {YELLOW}⚠  .sovereign_config.json: 'disable_bad_patterns' "
              f"must be a list{RESET}")
        _disable = []
    hints.load_packs(language=_pack_lang, packs=_packs, disable=_disable)

    # ── Extra bad patterns ────────────────────────────────────────────────────
    extra_bad = cfg.get("additional_bad_patterns", [])
    for entry in extra_bad:
        if isinstance(entry, dict) and "pattern" in entry and "hint" in entry:
            BAD_PATTERNS.append((_re.compile(entry["pattern"]), entry["hint"]))

    # ── Extra error hints ─────────────────────────────────────────────────────
    extra_hints = cfg.get("additional_error_hints", [])
    for entry in extra_hints:
        if isinstance(entry, dict) and "pattern" in entry and "hint" in entry:
            ERROR_HINTS.append((_re.compile(entry["pattern"]), entry["hint"]))

    # ── Always-include context files ──────────────────────────────────────────
    global PROJECT_ALWAYS_INCLUDE
    ai = cfg.get("context_always_include", [])
    if isinstance(ai, list) and all(isinstance(p, str) for p in ai):
        PROJECT_ALWAYS_INCLUDE = list(ai)
        if ai:
            print(f"  {DIM}Loaded {len(ai)} always-include context file(s){RESET}")

    # ── Per-project validation commands + language ────────────────────────────
    global PROJECT_VALIDATE_COMMANDS, PROJECT_LANGUAGE, PROJECT_ID
    vc = cfg.get("validate_commands", [])
    if isinstance(vc, list) and vc and all(isinstance(c, str) for c in vc):
        PROJECT_VALIDATE_COMMANDS = list(vc)
        print(f"  {DIM}Loaded {len(vc)} project validate command(s) — "
              f"built-in flutter/pytest/npm gates disabled{RESET}")
    lang = cfg.get("language")
    if isinstance(lang, str) and lang:
        PROJECT_LANGUAGE = lang.lower()

    proj_id = cfg.get("project")
    PROJECT_ID = proj_id if isinstance(proj_id, str) and proj_id else os.path.basename(
        os.path.abspath(project_root))

    total = len(exact) + len(prefixes)
    if total:
        print(f"  {DIM}Loaded project config: {len(exact)} locked paths, "
              f"{len(prefixes)} locked prefixes{RESET}")

    return cfg


# ─── Firestore bidirectional sync ─────────────────────────────────────────────

def _firestore_client():
    """Return a Firestore Client, or None if unavailable/unconfigured."""
    if not FIRESTORE_PROJECT:
        return None
    try:
        from google.cloud import firestore as _fs  # type: ignore
        return _fs.Client(project=FIRESTORE_PROJECT)
    except ImportError:
        print(f"  {YELLOW}google-cloud-firestore not installed — "
              f"pip install google-cloud-firestore{RESET}")
        return None
    except Exception as e:
        print(f"  {YELLOW}Firestore init failed: {e}{RESET}")
        return None


def _firestore_push_lessons(project_root: str) -> None:
    """Upload local error patterns, rule drafts, and velocity to Firestore.

    Uses merge=True so concurrent cloud workers never clobber each other.
    Collections written:
      error_patterns  — keyed by error code
      project_rules   — keyed by MD5 of rule text
      task_velocity   — keyed by MD5 of date+task (last 50 records)
    """
    db = _firestore_client()
    if db is None:
        return

    import hashlib as _hl2
    pushed = 0

    try:
        # ── 1. Error patterns ─────────────────────────────────────────────────
        errors_path = os.path.join(project_root, "logs", "errors.jsonl")
        if os.path.exists(errors_path):
            col = db.collection("error_patterns")
            with open(errors_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        code = (rec.get("code") or rec.get("error_code")
                                or rec.get("pattern", ""))
                        if not code:
                            continue
                        doc_id = f"{PROJECT_ID}_{code}".replace("/", "_")[:128]
                        col.document(doc_id).set({
                            "code":      code,
                            "count":     rec.get("count", 1),
                            "last_task": rec.get("task", "")[:120],
                            "last_date": rec.get("date", date.today().isoformat()),
                            "hint":      rec.get("hint", ""),
                            "source":    "local",
                            "project":   PROJECT_ID,
                        }, merge=True)
                        pushed += 1
                    except Exception:
                        pass

        # ── 2. Rule drafts ────────────────────────────────────────────────────
        rules_path = os.path.join(project_root, "logs", "rule_drafts.jsonl")
        if os.path.exists(rules_path):
            col = db.collection("project_rules")
            with open(rules_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        rule_text = rec.get("rule") or rec.get("new_rule", "")
                        if not rule_text:
                            continue
                        doc_id = f"{PROJECT_ID}_" + _hl2.md5(rule_text.encode()).hexdigest()[:16]
                        col.document(doc_id).set({
                            "rule":      rule_text,
                            "task_idx":  rec.get("task_idx", 0),
                            "date":      rec.get("date", date.today().isoformat()),
                            "source":    "local",
                            "applied":   False,
                            "project":   PROJECT_ID,
                        }, merge=True)
                        pushed += 1
                    except Exception:
                        pass

        # ── 3. Velocity (last 50 records — analytics) ─────────────────────────
        vel_path = os.path.join(project_root, VELOCITY_LOG)
        if os.path.exists(vel_path):
            col = db.collection("task_velocity")
            with open(vel_path) as f:
                lines = [l for l in f if l.strip()]
            for line in lines[-50:]:
                try:
                    rec = json.loads(line)
                    key = rec.get("date", "") + rec.get("task", "")[:60]
                    doc_id = f"{PROJECT_ID}_" + _hl2.md5(key.encode()).hexdigest()[:16]
                    col.document(doc_id).set({**rec, "project": PROJECT_ID}, merge=True)
                    pushed += 1
                except Exception:
                    pass

        if pushed:
            print(f"  {DIM}☁  Pushed {pushed} lesson record(s) to Firestore{RESET}")

    except Exception as e:
        print(f"  {DIM}(Firestore push failed: {e}){RESET}")


def _firestore_pull_lessons(project_root: str) -> None:
    """Download remote error patterns and rule drafts from Firestore.

    Merges cloud-learned patterns into local log files so this worker benefits
    from everything other workers (local or cloud) have already discovered.
    Safe to call repeatedly — skips records already present locally.
    """
    db = _firestore_client()
    if db is None:
        return

    print(f"  {DIM}↕  Pulling lessons from Firestore ({FIRESTORE_PROJECT})...{RESET}",
          end="", flush=True)
    pulled = 0

    try:
        # ── 1. Error patterns ─────────────────────────────────────────────────
        errors_path = os.path.join(project_root, "logs", "errors.jsonl")
        os.makedirs(os.path.dirname(errors_path), exist_ok=True)
        existing_codes: set[str] = set()
        if os.path.exists(errors_path):
            with open(errors_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        code = (rec.get("code") or rec.get("error_code")
                                or rec.get("pattern", ""))
                        if code:
                            existing_codes.add(code)
                    except Exception:
                        pass

        new_patterns: list[str] = []
        for doc in db.collection("error_patterns").where("project", "==", PROJECT_ID).stream():
            d = doc.to_dict()
            code = d.get("code", "")
            if code and code not in existing_codes:
                new_patterns.append(json.dumps(d))
                existing_codes.add(code)
                pulled += 1

        if new_patterns:
            with open(errors_path, "a") as f:
                for line in new_patterns:
                    f.write(line + "\n")

        # ── 2. Rule drafts ────────────────────────────────────────────────────
        rules_path = os.path.join(project_root, "logs", "rule_drafts.jsonl")
        existing_rules: set[str] = set()
        if os.path.exists(rules_path):
            with open(rules_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        rule = rec.get("rule") or rec.get("new_rule", "")
                        if rule:
                            existing_rules.add(rule[:80])
                    except Exception:
                        pass

        new_rules: list[str] = []
        for doc in db.collection("project_rules").where("project", "==", PROJECT_ID).stream():
            d = doc.to_dict()
            rule = d.get("rule", "")
            if rule and rule[:80] not in existing_rules:
                new_rules.append(json.dumps(d))
                existing_rules.add(rule[:80])
                pulled += 1

        if new_rules:
            with open(rules_path, "a") as f:
                for line in new_rules:
                    f.write(line + "\n")

        print(f" {DIM}{pulled} new record(s){RESET}")

    except Exception as e:
        print(f" {YELLOW}failed: {e}{RESET}")


def backup_files(paths: list[str], project_root: str) -> dict[str, str]:
    return {p: open(os.path.join(project_root, p)).read()
            for p in paths if os.path.exists(os.path.join(project_root, p))}


def restore_files(backups: dict[str, str], project_root: str):
    for rel, content in backups.items():
        with open(os.path.join(project_root, rel), "w") as f:
            f.write(content)


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
    if PROJECT_VALIDATE_COMMANDS:
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


_TASK_GATE_RE = re.compile(r"task gate:\s*(.+?)(?:\s+passes)?\s*$", re.IGNORECASE)


def _extract_task_gate(task: str | None) -> str | None:
    """Pull the per-task acceptance command out of the task text.

    Tasks are authored with a trailing "task gate: <cmd> passes" clause (see
    plan_week.py). That command is the REAL acceptance test — typically a
    scoped conformance run like `npx vitest run conformance/pN.test.ts -t 'B05'`.
    validate_commands (typecheck + test:locked) pass on unimplemented stubs, so
    without running this gate a stub logs as PASSED. Returns the command, or
    None if the task carries no gate clause.
    """
    if not task:
        return None
    for line in task.splitlines():
        m = _TASK_GATE_RE.search(line.strip())
        if m:
            cmd = m.group(1).strip()
            return cmd or None
    return None


def validate(
    baseline_errors: frozenset[str] | None = None,
    files_written: list[str] | None = None,
    task_gate: str | None = None,
) -> tuple[bool, str]:
    # ── Per-project gates (from .sovereign_config.json "validate_commands") ──
    # Run in order, cwd = project root (main() chdirs there at startup).
    # First nonzero exit fails the task with that command's output.
    if PROJECT_VALIDATE_COMMANDS:
        for shell_cmd in PROJECT_VALIDATE_COMMANDS:
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


# ─── Time budget helpers ─────────────────────────────────────────────────────

def compute_task_budget(project_root: str,
                        multiplier: float = BUDGET_MULTIPLIER,
                        ceiling_s: float = BUDGET_CEILING_S) -> float:
    """Return the time budget in seconds for the next task.

    Uses the rolling average of recently completed tasks × multiplier,
    capped at ceiling_s. Falls back to ceiling_s until there's enough history.

    First pass : multiplier=1.7, ceiling_s=600  (≈10 min when avg is 6 min)
    Retry pass : multiplier=3.4, ceiling_s=1200 (≈20 min when avg is 6 min)
    """
    vel_path = os.path.join(project_root, VELOCITY_LOG)
    if not os.path.exists(vel_path):
        return ceiling_s
    durations: list[float] = []
    with open(vel_path) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("outcome") == "done":
                    durations.append(float(r["duration_s"]))
            except Exception:
                pass
    if len(durations) < 5:
        return ceiling_s   # not enough history yet — be generous
    recent = durations[-BUDGET_SAMPLES:]
    avg = sum(recent) / len(recent)
    return max(BUDGET_FLOOR_S, min(avg * multiplier, ceiling_s))


def print_timing_report(project_root: str, session_start: float) -> None:
    """Print a timing summary from velocity.jsonl."""
    vel_path = os.path.join(project_root, VELOCITY_LOG)
    if not os.path.exists(vel_path):
        return
    records: list[dict] = []
    with open(vel_path) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    if not records:
        return

    done    = [r for r in records if r.get("outcome") == "done"]
    skipped = [r for r in records if r.get("outcome") in ("skipped", "budget", "failed")]

    print(f"\n{BOLD}{'━'*56}{RESET}")
    print(f"{BOLD}  ⏱  Timing Report{RESET}")
    if done:
        durations = sorted(r["duration_s"] for r in done)
        avg  = sum(durations) / len(durations)
        p50  = durations[len(durations) // 2]
        p90  = durations[max(0, int(len(durations) * 0.9) - 1)]
        budget = compute_task_budget(project_root)
        print(f"  Completed       : {len(done)} tasks")
        print(f"  Avg time/task   : {avg:.0f}s  ({avg/60:.1f} min)")
        print(f"  Median (p50)    : {p50:.0f}s")
        print(f"  p90             : {p90:.0f}s")
        print(f"  Fastest         : {durations[0]:.0f}s")
        print(f"  Slowest         : {durations[-1]:.0f}s")
        print(f"  Current budget  : {budget:.0f}s ({budget/60:.1f} min)  "
              f"[avg × {BUDGET_MULTIPLIER}]")
    if skipped:
        budget_hits = sum(1 for r in records if r.get("outcome") == "budget")
        print(f"  Skipped/failed  : {len(skipped)}"
              + (f"  ({budget_hits} by time budget)" if budget_hits else ""))
    session_mins = (time.time() - session_start) / 60
    print(f"  Session time    : {session_mins:.1f} min")
    print(f"{BOLD}{'━'*56}{RESET}\n")


# ─── Task runner ─────────────────────────────────────────────────────────────

VELOCITY_LOG   = "logs/velocity.jsonl"
ESCALATE_LOG   = "logs/escalate.md"
TRACES_LOG     = "logs/task_traces.jsonl"
RACE_LOG       = "logs/race.jsonl"

# ── Firestore sync ─────────────────────────────────────────────────────────────
# Set FIRESTORE_PROJECT_ID in .env to enable bidirectional sync with cloud workers.
# Requires: pip install google-cloud-firestore
# Auth:      GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json  OR  gcloud auth
FIRESTORE_PROJECT = os.getenv("FIRESTORE_PROJECT_ID", "")

# After this many attempts with a structurally-unfixable error, stop burning
# retries and escalate to Claude instead.
ESCALATE_AFTER = 3

# If a task appears in velocity.jsonl with outcome "skipped" or "failed" this many
# times, bypass all local model tiers and send it straight to Claude.  Set to 0
# to disable (every task starts at tier 1 every run regardless of history).
CHRONIC_BLOCKER_THRESHOLD = int(os.getenv("CHRONIC_THRESHOLD", "1"))

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


def _append_velocity(project_root: str, record: dict):
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


def _write_escalation(project_root: str, task_idx: int, task: str,
                      attempt: int, error: str,
                      written: list[str], skipped: list[str]) -> str:
    """Write a rich escalation report for Claude to act on. Returns the path."""
    path = os.path.join(project_root, ESCALATE_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Categorise the likely cause
    if _re.search(r'uri_does_not_exist', error):
        cause = "Missing package — `flutter pub get` may not have been run, or package absent from pubspec.yaml."
    elif _re.search(r'annotate_overrides|field_shadows_inherited', error):
        cause = "Pre-existing lint in a model file (star.dart, mote.dart, etc.) unrelated to this task."
    elif not written:
        cause = "All candidate files are LOCKED — 35B produced correct code but had nowhere to write it."
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
    _global_rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global_rules.md")
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
    _global_rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global_rules.md")
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
    import hashlib as _hl

    _max_tier = max_tier_idx if max_tier_idx is not None else len(TIER_MODELS)

    task_start    = time.time()
    errors_seen: list[str] = []
    last_written: list[str] = []
    last_skipped: list[str] = []

    # Tier state — honour start_tier_idx for deep/resume runs
    tier_idx: int  = min(start_tier_idx, _max_tier - 1)
    current_model  = TIER_MODELS[tier_idx]
    phase_stale: int        = 0   # same-error streak within current tier
    phase_attempts: int     = 0   # total validation failures in current tier (catches thrashing)
    consecutive_bad_pattern: int = 0  # bad-pattern blocks in a row; escalate after limit
    last_err_sig   = ""

    is_test = _is_test_task(task)
    call_info: dict = {}    # populated by implement_task on each attempt
    context_trimmed = False  # True after first prompt-too-large trim

    print(f"  {DIM}Finding relevant files ({PLANNER_MODEL})...{RESET}")
    rel_files = find_relevant_files(task, project_root)

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
    if PROJECT_LANGUAGE == "go":
        _dirs: set[str] = set()
        _tm = re.search(r'\bIn ([\w./-]+\.go):', task)
        if _tm and os.path.dirname(_tm.group(1)) not in ("", "sim"):
            _dirs.add(os.path.dirname(_tm.group(1)))
        for _pkg in set(re.findall(r'\b([a-z]\w+)\.[A-Z]\w*', task)) - {"sim"}:
            if os.path.isdir(os.path.join(project_root, _pkg)):
                _dirs.add(_pkg)
        for _d in sorted(_dirs):
            _fs = sorted(f for f in os.listdir(os.path.join(project_root, _d))
                         if f.endswith(".go"))[:8]
            pkg_ctx += [os.path.join(_d, f) for f in _fs]
    forced = [f for f in PROJECT_ALWAYS_INCLUDE + spec_refs + ref_refs + pkg_ctx
              if os.path.exists(os.path.join(project_root, f))]
    rel_files = list(dict.fromkeys(forced + rel_files))
    print(f"  Files: {', '.join(rel_files) or '(none found)'}")

    # ── Dependency-abort guard (TS projects) ──────────────────────────────────
    # If the task names sibling project modules that don't exist yet, it can't
    # pass — the worker would just hallucinate the missing symbol and burn every
    # attempt (observed: GameCanvas run before useGameLoop.ts landed → "Cannot
    # find name 'useGameLoop'"). Defer to the retry pass so the dependency can
    # land first, instead of failing the task outright.
    if PROJECT_LANGUAGE in ("typescript", "ts", "tsx"):
        _missing = _missing_local_deps(task, project_root)
        if _missing:
            print(f"  {YELLOW}⏭  Deferred: depends on file(s) not yet present — "
                  f"{', '.join(_missing)}{RESET}")
            print(f"     (will be retried after its dependencies land)")
            _append_velocity(project_root, {
                "date": date.today().isoformat(), "task": task[:120],
                "outcome": "skipped", "attempts": 0,
                "duration_s": round(time.time() - task_start, 1),
                "error_types": ["missing_dependency"],
            })
            return "skipped"

    # Snapshot pre-existing errors so the validator only fails on NEW ones
    baseline_errors = snapshot_baseline_errors(project_root)

    backups = backup_files(rel_files, project_root)
    errors  = ""

    # ── Chronic blocker fast-path ─────────────────────────────────────────────
    # If this task has been skipped in previous runs, skip local models entirely
    # and call Claude directly with a richer, focused prompt.  If Claude also
    # fails, fall through to the normal tier loop as a last resort.
    if CLAUDE_ENABLED and CHRONIC_BLOCKER_THRESHOLD > 0:
        prior_skips = _count_prior_skips(task, project_root)
        if prior_skips >= CHRONIC_BLOCKER_THRESHOLD:
            file_contents_c = read_files(rel_files, project_root)
            claude_changes, claude_ok = _escalate_to_claude_chronic(
                task, file_contents_c, prior_skips, project_root, is_test)
            if claude_changes:
                written_c, pat_errs_c = write_changes(
                    claude_changes, project_root, test_only=is_test)
                if not pat_errs_c:
                    passed_c, output_c = validate(baseline_errors, files_written=written_c, task_gate=_extract_task_gate(task))
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
        """Move to the next model tier. Returns False if all tiers exhausted."""
        nonlocal tier_idx, current_model, phase_stale, phase_attempts, last_err_sig, errors
        prev_model = TIER_MODELS[tier_idx]
        tier_idx += 1
        if tier_idx >= _max_tier:
            return False
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
            errors = f"Model timed out or connection failed: {exc}. Retry."
            errors_seen.append("timeout")
            print(f" {YELLOW}timeout — retrying{RESET}")
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
        if PROJECT_LANGUAGE == "go":
            _m = re.search(r'\bIn ([\w./-]+\.go):', task)
            _go_target = _m.group(1) if _m else None
        written, pattern_errs = write_changes(changes, project_root, test_only=is_test,
                                              required_file=_go_target)
        last_written = written

        if pattern_errs:
            errors = pattern_errs + ("\n\n" + errors if errors else "")
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

        consecutive_bad_pattern = 0   # model wrote real code — reset bad-pattern streak
        changed_list = ", ".join(written)
        print(f" wrote {len(written)} file(s): {changed_list[:60]}")

        # Pre-format Go files BEFORE the first validation (2026-07-13).
        # Local models emit space-indented Go, so validate #1 always died at
        # the gofmt gate, and the post-autofix failure produced a different
        # error signature each attempt — feeding the thrash detector bogus
        # "all different errors" streaks and escalating tiers prematurely.
        if PROJECT_LANGUAGE == "go":
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

        print(f"  {DIM}Validating...{RESET}", end="", flush=True)
        passed, output = validate(baseline_errors, files_written=written, task_gate=_extract_task_gate(task))

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

        # Step 1: Mechanical auto-fixer
        try:
            af = _get_autofix()
            if PROJECT_LANGUAGE == "go":
                fix_count, _ = af.apply_go_mechanical_fixes(project_root, written)
            else:
                fix_count, _ = af.apply_mechanical_fixes(output, project_root)
            if fix_count > 0:
                print(f"  {DIM}⚙  Auto-fixed {fix_count} issue(s) — re-validating...{RESET}", end="", flush=True)
                passed2, output2 = validate(baseline_errors, files_written=written, task_gate=_extract_task_gate(task))
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

                if CLAUDE_ENABLED:
                    restore_files(backups, project_root)  # clean slate for Claude
                    file_contents_claude = read_files(rel_files, project_root)
                    claude_changes, _ = _escalate_to_claude(
                        task, file_contents_claude, errors, project_root, is_test)
                    if claude_changes:
                        written_c, pat_errs_c = write_changes(
                            claude_changes, project_root, test_only=is_test)
                        if not pat_errs_c:
                            passed_c, output_c = validate(baseline_errors, files_written=written_c, task_gate=_extract_task_gate(task))
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
                                  last_written, last_skipped)
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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
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
    args = parser.parse_args()

    if args.project:
        project_root = os.path.abspath(args.project)
        if not os.path.isdir(project_root):
            print(f"⚠  Not found: {project_root}")
            sys.exit(1)
        os.chdir(project_root)
    else:
        project_root = os.getcwd()

    # ── Pre-run git hygiene guards ───────────────────────────────────────────
    # Skipped for the pure counting queries (--remaining-count /
    # --queue-remaining-count): supervisor.sh captures their stdout as an
    # integer, so these must print nothing and never exit early.
    #   1. Must be on the integration branch (default: main). Starting a run
    #      from a leftover task-* branch is what stranded every earlier fix on
    #      an abandoned branch and left main behind. Override with MAIN_BRANCH.
    #   2. Working tree must be clean, EXCEPT for logs/ (generated each run and
    #      gitignored per project — they must never block or dirty a run).
    if not (args.remaining_count or args.queue_remaining_count) \
            and os.path.exists(os.path.join(project_root, ".git")):
        main_branch = os.getenv("MAIN_BRANCH", "main")
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

    # ── Load per-project config (.sovereign_config.json) ─────────────────────
    # Must happen before anything else so LOCKED_FILES is populated before
    # any file reads, writes, or planning steps touch the project.
    # EXCEPT for the counting queries below: supervisor.sh does
    # `N=$(work.py --remaining-count ...)` and tests N as an integer, so
    # stdout must contain ONLY the number. _load_project_config prints
    # "Loaded N always-include context file(s)" etc., which polluted the
    # capture and made supervisor.sh's [ -eq ] test fail
    # ("integer expression expected") — it then fell through to a false
    # "All ROADMAP tasks complete". Counting uses only parse_all_tasks()/
    # _is_test_task(), neither of which needs the config, so config load is
    # deferred until after the counting branches exit. (2026-07-14)
    if not (args.remaining_count or args.queue_remaining_count):
        _load_project_config(project_root)

        # ── Environment preflight ────────────────────────────────────────
        # 2026-07-13: 76 consecutive failures on a project where
        # `package:flutter/material.dart` did not resolve. No model can fix an
        # unbuilt dependency tree; every attempt was doomed before the first
        # token. One stat() up front turns a wasted run into a clear message.
        try:
            _problems = grounders.for_language(PROJECT_LANGUAGE).preflight(project_root)
            for _p in _problems:
                print(f"  {YELLOW}⚠  PREFLIGHT: {_p}{RESET}")
            if _problems:
                print(f"  {YELLOW}   Fix the above before running — model "
                      f"attempts cannot succeed against a broken tree.{RESET}")
                _ledger(project_root, -1, "-", "preflight_failed",
                        "; ".join(_problems))
        except Exception:
            pass  # preflight is advisory; never block a run on it

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

    # ── Sprint planning commit (--commit-sprint) ──────────────────────────────
    # Must run before the task loop so cloud workers can pull ROADMAP.md + DAG.
    if args.commit_sprint:
        print(f"\n{BOLD}  📦  Committing sprint plan to git...{RESET}")
        _post_planning_commit(project_root)

    # ── Firestore pull: merge remote lessons before we start ──────────────────
    # Gives this worker benefit of everything other workers have already learned.
    if FIRESTORE_PROJECT:
        _firestore_pull_lessons(project_root)

    # ── Dependency check ──────────────────────────────────────────────────────
    # Run flutter pub get if pubspec.yaml is newer than pubspec.lock, or if
    # .dart_tool/package_config.json is missing (fresh clone / stale cache).
    pubspec_yaml = os.path.join(project_root, "pubspec.yaml")
    pubspec_lock = os.path.join(project_root, "pubspec.lock")
    pkg_config   = os.path.join(project_root, ".dart_tool", "package_config.json")
    if os.path.exists(pubspec_yaml):
        needs_pub_get = (
            not os.path.exists(pkg_config)
            or not os.path.exists(pubspec_lock)
            or os.path.getmtime(pubspec_yaml) > os.path.getmtime(pubspec_lock)
        )
        if needs_pub_get:
            if args.worker_id == 0:
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
                print(f"  {YELLOW}worker {args.worker_id}: skipping flutter pub get (worker 0 handles it){RESET}")

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

    # ── Parallel worker config ────────────────────────────────────────────────
    # When stride > 1 each worker handles every Nth task (round-robin).
    # Per-worker suffixes keep log and escalation files separate.
    worker_suffix = f"-w{args.worker_id}" if args.stride > 1 else ""
    if args.stride > 1:
        global ESCALATE_LOG
        ESCALATE_LOG = f"logs/escalate{worker_suffix}.md"

    today_str = date.today().isoformat()

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

    tasks = parse_all_tasks()

    # ── Quick mode: skip tasks already queued for --deep ──────────────────────
    # If tier2_queue.jsonl exists from a prior T1 run, those tasks already
    # failed at tier1. Only skip them if we're re-running from T1 (start=0) —
    # if we're starting at T2+, those queued tasks ARE what we want to process.
    if (args.quick or run_max_tier_idx is not None) and run_start_tier_idx == 0:
        _prior_queue_file = os.path.join(project_root, TIER2_QUEUE_FILE)
        if os.path.exists(_prior_queue_file):
            _already_queued: set[str] = set()
            with open(_prior_queue_file) as _pqf:
                for _line in _pqf:
                    try:
                        _already_queued.add(json.loads(_line)["task"])
                    except (json.JSONDecodeError, KeyError):
                        pass
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
        queued: set[str] = set()
        with open(queue_file) as _qf:
            for _line in _qf:
                try:
                    queued.add(json.loads(_line)["task"])
                except (json.JSONDecodeError, KeyError):
                    pass
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

    # ── Chain-aware task assignment ────────────────────────────────────────────
    # Two kinds of adjacency in ROADMAP.md's task order must never be split
    # across parallel workers, or the tasks race each other with no
    # coordination:
    #   (a) consecutive tasks targeting the same primary file (one worker can
    #       leave the file mid-edit for another to inherit)
    #   (b) an "implement" task immediately followed by its test task (the
    #       test would run against a possibly-missing implementation)
    # Chains are built from the FULL, unfiltered task list first; stride is
    # then applied at the chain level (round-robin over whole chains) instead
    # of the raw task index, so a chain always lands on exactly one worker.
    MAX_CHAIN_LEN = 6  # cap to avoid giant prompts / one worker hoarding tasks

    import re as _re2
    def _primary_file(t: str) -> str | None:
        m = _re2.search(r'\b(lib/[\w/]+\.dart|test/[\w/]+\.dart)\b', t)
        return m.group(1) if m else None

    indexed_tasks_all = [(i + 1, t) for i, t in enumerate(tasks)]

    chains: list[list[tuple[int, str]]] = []
    i = 0
    while i < len(indexed_tasks_all):
        idx, task_line = indexed_tasks_all[i]
        pf = _primary_file(task_line)
        chain = [(idx, task_line)]
        j = i + 1
        while j < len(indexed_tasks_all) and j - i < MAX_CHAIN_LEN:
            nidx, ntask = indexed_tasks_all[j]
            same_file  = pf is not None and _primary_file(ntask) == pf
            is_test_of = _is_test_task(task_text(ntask)) and not _is_test_task(task_text(indexed_tasks_all[j - 1][1]))
            if same_file or is_test_of:
                chain.append((nidx, ntask))
                j += 1
            else:
                break
        chains.append(chain)
        i = j

    if args.stride > 1:
        chains = [c for k, c in enumerate(chains) if k % args.stride == args.worker_id]

    # Flatten assigned chains back into an ordered task list (same relative
    # order as ROADMAP.md, restricted to this worker's chains).
    indexed_tasks = [item for chain in chains for item in chain]

    # NOTE (2026-07-10): same-file tasks used to be merged here into one giant
    # "[COMPOUND — implement ALL N sub-tasks...]" LLM call. Removed — bundling
    # N small tasks into one oversized generation call directly fights the
    # "smallest possible task" principle, and empirically made tasks HARDER
    # to complete, not safer (a 5-subtask compound call on one file failed
    # across all 4 tiers for over an hour, when 5 separate small edits would
    # each have had a real chance). Same-file tasks stay small and separate;
    # chain-building above still keeps them on one worker, in order, and the
    # same-file cascade guard below (extending the old test-dependency block)
    # replaces the safety compound-merging used to provide.
    _compound_extras: dict[str, list[str]] = {}  # kept empty; mark_done() checks it unconditionally

    if not indexed_tasks:
        print(f"{YELLOW}No unchecked tasks found anywhere in ROADMAP.md.{RESET}")
        print(f"Add tasks to ROADMAP.md or run plan_week.py first.")
        sys.exit(0)

    # Disable Claude escalation in --quick and --deep runs to save API costs
    # and keep these passes purely local-model based.
    if args.quick or args.deep:
        global CLAUDE_ENABLED
        CLAUDE_ENABLED = False

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

    # ── Verify Model Availability ─────────────────────────────────────────────
    # Fast check at startup to ensure the cascade won't fail halfway through a run
    if not args.dry_run:
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

    os.makedirs(LOG_DIR, exist_ok=True)

    log_file = os.path.join(LOG_DIR, f"{today_str}-work{worker_suffix}.log")
    with open(log_file, "w") as f:
        f.write(f"# Work log — {today_str}\nProject: {project_root}\n"
                + (f"Worker: {args.worker_id}/{args.stride}\n" if args.stride > 1 else ""))

    session_start = time.time()
    passed_count  = 0
    failed_count  = 0
    skipped_count = 0
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
    go_failed_targets: set[str] = set()
    go_task_deps: dict[int, list[tuple[int, str]]] = {}
    if PROJECT_LANGUAGE == "go":
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
            skipped_count += 1
            skipped_tasks.append((task_idx, task))
            retry_queue.append((task_idx, task_line, task))
            last_incomplete = task_idx
            last_incomplete_file = _primary_file(task_line)
            _ledger(project_root, task_idx, "-", "task_dep_blocked", _dt)
            continue

        # Dependency block: defer this task if the immediately preceding task
        # didn't complete AND either (a) this is its test, or (b) this targets
        # the same file (would inherit whatever broken state was left behind).
        is_immediate_successor = last_incomplete is not None and task_idx == last_incomplete + 1
        same_file_as_failure = (
            is_immediate_successor
            and last_incomplete_file is not None
            and _primary_file(task_line) == last_incomplete_file
        )
        if is_immediate_successor and (_is_test_task(task) or same_file_as_failure):
            reason = "same file as the failed task" if same_file_as_failure else "test of a task that didn't complete"
            print(
                f"  {YELLOW}⏭  Dependency-blocked: task {last_incomplete} did not complete "
                f"({reason}) — deferred to retry pass{RESET}"
            )
            with open(log_file, "a") as f:
                f.write(f"Dependency-blocked: preceding task {last_incomplete} failed/skipped ({reason}).\n")
            skipped_count += 1
            skipped_tasks.append((task_idx, task))
            retry_queue.append((task_idx, task_line, task))
            last_incomplete = task_idx   # chain: mark this task as incomplete too
            last_incomplete_file = _primary_file(task_line)
            continue

        branch = _branch_start(project_root, task_idx, task)
        if branch:
            print(f"  {DIM}⎇  Branch: {branch}{RESET}")

        result = run_task(task, project_root, log_file, task_idx=task_idx,
                         budget_s=get_first_pass_budget(),
                         max_tier_idx=run_max_tier_idx,
                         start_tier_idx=run_start_tier_idx,
                         skip_advisor=getattr(args, 'no_advisor', False))

        if result is True:
            if branch:
                _branch_merge(project_root, branch, task_idx, task)
                print(f"  {DIM}⎇  Merged {branch} → main{RESET}")
            # For compound tasks, mark all constituent task lines done.
            # For regular tasks, _compound_extras won't have an entry and mark_done
            # is called once on task_line as before.
            extra_lines = _compound_extras.get(task, [])
            if extra_lines:
                for orig_line in extra_lines:
                    mark_done(orig_line)
                print(f"  {GREEN}✓ Marked {len(extra_lines)} compound sub-tasks done in ROADMAP.md{RESET}")
            else:
                mark_done(task_line)
                print(f"  {GREEN}✓ Marked done in ROADMAP.md{RESET}")
            passed_count += 1
            _tm = re.search(r'\bIn ([\w./-]+\.go):', task)
            if _tm:
                go_failed_targets.discard(_tm.group(1))
            last_incomplete = None        # successful task resets the chain
            last_incomplete_file = None
        elif result == "skipped":
            if branch:
                _branch_abandon(project_root, branch)
            # In a capped run (--quick / --max-tier), write failures to the
            # tier2 queue so a follow-up --deep run can pick them up.
            if run_max_tier_idx is not None and run_max_tier_idx < len(TIER_MODELS):
                _write_tier2_queue(project_root, task_idx, task, [])
                print(f"  {YELLOW}↳ queued for --deep run{RESET}")
            skipped_count += 1
            skipped_tasks.append((task_idx, task))
            retry_queue.append((task_idx, task_line, task))
            last_incomplete = task_idx
            last_incomplete_file = _primary_file(task_line)
            _tm = re.search(r'\bIn ([\w./-]+\.go):', task)
            if _tm:
                go_failed_targets.add(_tm.group(1))
            print(f"  {YELLOW}⏭  Skipped — queued for retry pass{RESET}")
        else:
            if branch:
                _branch_abandon(project_root, branch)
            failed_count += 1
            retry_queue.append((task_idx, task_line, task))
            last_incomplete = task_idx
            last_incomplete_file = _primary_file(task_line)
            _tm = re.search(r'\bIn ([\w./-]+\.go):', task)
            if _tm:
                go_failed_targets.add(_tm.group(1))
            print(f"  {YELLOW}Continuing to next task...{RESET}")

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

    # ── Automatic retry pass ──────────────────────────────────────────────────
    if retry_queue and not args.dry_run and not args.no_retry:
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

            result = run_task(task, project_root, log_file,
                              task_idx=task_idx, budget_s=retry_budget,
                              skip_advisor=getattr(args, 'no_advisor', False))
            if result is True:
                if branch:
                    _branch_merge(project_root, branch, task_idx, task)
                    print(f"  {DIM}⎇  Merged {branch} → main{RESET}")
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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Interrupted — progress saved to ROADMAP.md{RESET}\n")
        sys.exit(130)
