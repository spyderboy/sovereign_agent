"""Runtime configuration for the sovereign worker loop.

Everything that decides WHICH models run, HOW MANY attempts they get, and HOW
LONG a task may take. work.py imports these names and does not define them.

Precedence, highest first:

    1. environment variable      TIER1_MODEL=gemma4:26b ./supervisor.sh ...
    2. profile file              profiles/<name>.toml, chosen by SOVEREIGN_PROFILE
    3. built-in default          the values below

Environment wins so every existing `.env` file keeps working untouched. A
profile is just a nicer way to write the same settings down and give them a
name.

    SOVEREIGN_PROFILE=gemma26 ./supervisor.sh ~/Code/witches_bricks --features-only

WHY THIS EXISTS

Changing the model ladder used to mean editing work.py — a 4,000-line file that
is also the engine of every run. Two costs followed from that.

The comments rotted immediately. Before this module, work.py read:

    TIER1_MODEL = os.getenv("TIER1_MODEL", "qwen2.5-coder:32b")  # 7B dense
    TIER2_MODEL = os.getenv("TIER2_MODEL", "qwen2.5-coder:32b")  # 26B MoE (4B active)
    TIER3_MODEL = os.getenv("TIER3_MODEL", "qwen2.5-coder:32b")  # 30B dense

Four tiers, one model, three comments describing models that were not there.

And it put experiments in the blast radius of the engine. An A/B on model choice
became a commit titled "temp: manual edits for Nb run" against work.py, which is
exactly the kind of change that gets reverted in a hurry and takes unrelated work
with it. A profile is data: copy the file, change two lines, run both.
"""

from __future__ import annotations

import os
import sys

try:                                    # 3.11+
    import tomllib
except ModuleNotFoundError:             # pragma: no cover
    tomllib = None

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(_HERE, "profiles")
PROFILE_NAME = os.getenv("SOVEREIGN_PROFILE", "")


def _load_profile(name: str) -> dict:
    """Read profiles/<name>.toml. Absent or unparseable -> {} and a warning.

    Deliberately non-fatal. A typo in a profile name should fall back to the
    built-in defaults with a loud line, not stop a run that was about to work.
    """
    if not name:
        return {}
    path = os.path.join(PROFILE_DIR, f"{name}.toml")
    if not os.path.exists(path):
        print(f"⚠  profile '{name}' not found at {path} — using defaults",
              file=sys.stderr)
        return {}
    if tomllib is None:
        print("⚠  python < 3.11 has no tomllib — profiles ignored", file=sys.stderr)
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"⚠  profile '{name}' failed to parse ({e}) — using defaults",
              file=sys.stderr)
        return {}


_P = _load_profile(PROFILE_NAME)
_TIERS = _P.get("tiers", {})
_BUDGET = _P.get("budget", {})
_RUN = _P.get("run", {})


def _tier(idx: int, key: str, default):
    """A tier setting from the profile: [tiers.1] model = "..." """
    t = _TIERS.get(str(idx)) or _TIERS.get(idx) or {}
    return t.get(key, default)


def _s(env: str, profile_val, default: str) -> str:
    return os.getenv(env) or (profile_val if profile_val is not None else default)


def _i(env: str, profile_val, default: int) -> int:
    v = os.getenv(env)
    if v is not None:
        return int(v)
    return int(profile_val) if profile_val is not None else default


def _f(env: str, profile_val, default: float) -> float:
    v = os.getenv(env)
    if v is not None:
        return float(v)
    return float(profile_val) if profile_val is not None else default


def _b(env: str, profile_val, default: bool) -> bool:
    v = os.getenv(env)
    if v is not None:
        return v == "1"
    return bool(profile_val) if profile_val is not None else default


# ── Endpoints and repo ───────────────────────────────────────────────────────
OLLAMA_URL = _s("LOCAL_MODEL_URL", _RUN.get("ollama_url"),
                "http://localhost:11434")

# Integration branch. Tasks fork from and merge back to this. Override for
# projects whose integration branch isn't 'main' (galaxican uses 'master').
# The branch helpers MUST use this, not a hardcoded 'main', or work merges to a
# divergent 'main' while the real integration branch is left behind.
MAIN_BRANCH = _s("MAIN_BRANCH", _RUN.get("main_branch"), "main")

# ── The ladder ───────────────────────────────────────────────────────────────
TIER1_MODEL = _s("TIER1_MODEL", _tier(1, "model", None), "qwen2.5-coder:14b")
TIER2_MODEL = _s("TIER2_MODEL", _tier(2, "model", None), "qwen2.5-coder:32b")
TIER3_MODEL = _s("TIER3_MODEL", _tier(3, "model", None), "qwen3.6:35b-a3b-coding-nvfp4")
TIER4_MODEL = _s("TIER4_MODEL", _tier(4, "model", None), "qwen3.6:35b-a3b-coding-nvfp4")
TIER_MODELS = [TIER1_MODEL, TIER2_MODEL, TIER3_MODEL, TIER4_MODEL]

# ── Quick-mode parameter gate ────────────────────────────────────────────────
# --quick may only use models under this total parameter count, enforced via
# QUICK_MAX_TIER_IDX. Unknown models default to inf, so they are excluded.
QUICK_PARAM_LIMIT_B: float = _f("QUICK_PARAM_LIMIT_B",
                                _BUDGET.get("quick_param_limit_b"), 30.0)

MODEL_PARAMS: dict[str, float] = {
    TIER1_MODEL: _f("MODEL_PARAMS_TIER1", _tier(1, "params", None), 14.0),
    TIER2_MODEL: _f("MODEL_PARAMS_TIER2", _tier(2, "params", None), 32.0),
    TIER3_MODEL: _f("MODEL_PARAMS_TIER3", _tier(3, "params", None), 35.0),
    TIER4_MODEL: _f("MODEL_PARAMS_TIER4", _tier(4, "params", None), 35.0),
}

# ── Support models ───────────────────────────────────────────────────────────
# These default to TIER1_MODEL rather than a hardcoded 7B. work.py only calls
# the advisor at tier 1 precisely to avoid evicting a larger model mid-run; if
# the advisor is a DIFFERENT model from tier 1 that saving is lost, because the
# advisor call itself forces a swap. Defaulting to tier 1 makes the common path
# free by construction instead of by coincidence.
PLANNER_MODEL = _s("PLANNER_MODEL", _RUN.get("planner_model"), TIER1_MODEL)
ADVISOR_MODEL = _s("ADVISOR_MODEL", _RUN.get("advisor_model"), TIER1_MODEL)
RACE_MODEL    = _s("RACE_MODEL",    _RUN.get("race_model"),    TIER1_MODEL)
RACE_ENABLED  = _b("RACE_ENABLED",  _RUN.get("race_enabled"),  False)
GIT_BRANCHES  = _b("GIT_BRANCHES",  _RUN.get("git_branches"),  True)

CLAUDE_MODEL   = _s("CLAUDE_MODEL",   _RUN.get("claude_model"), "claude-sonnet-4-6")
CLAUDE_ENABLED = _b("CLAUDE_ENABLED", _RUN.get("claude_enabled"), False)

# ── Context windows ──────────────────────────────────────────────────────────
# Keyed by model name, so two tiers sharing weights share one entry — which is
# why setting CTX_TIER1 and CTX_ADVISOR differently for the same model silently
# does nothing. _ctx_conflicts() reports that instead of leaving it to be
# discovered.
_CTX_DEFAULT = _i("CTX_DEFAULT", _RUN.get("ctx_default"), 24576)
_TIER1_CTX = _i("CTX_TIER1", _tier(1, "ctx", None), _CTX_DEFAULT)

# Support models default to the TIER 1 context, not the generic default.
#
# They also default to TIER1_MODEL, so all four names usually collide on one
# dict key — and a dict literal lets the LAST entry win. Defaulting the support
# entries to _CTX_DEFAULT therefore silently overwrote the tier's own setting:
# a profile asking for ctx 16384 got 24576, because planner/advisor/race were
# written after it with the fallback. Tier entries are applied LAST below so
# an explicit tier setting always wins, and the support fallback matches tier 1
# so the collision is harmless even when it happens.
MODEL_CTX: dict[str, int] = {
    PLANNER_MODEL: _i("CTX_PLANNER", _RUN.get("ctx_planner"), _TIER1_CTX),
    ADVISOR_MODEL: _i("CTX_ADVISOR", _RUN.get("ctx_advisor"), _TIER1_CTX),
    RACE_MODEL:    _i("CTX_RACE",    _RUN.get("ctx_race"),    _TIER1_CTX),
}
MODEL_CTX.update({
    TIER1_MODEL: _TIER1_CTX,
    TIER2_MODEL: _i("CTX_TIER2", _tier(2, "ctx", None), _CTX_DEFAULT),
    TIER3_MODEL: _i("CTX_TIER3", _tier(3, "ctx", None), _CTX_DEFAULT),
    TIER4_MODEL: _i("CTX_TIER4", _tier(4, "ctx", None), _CTX_DEFAULT),
})


def ctx_conflicts() -> str:
    """Warn when several context settings collapse onto one model entry."""
    shared = [n for n, m in (("CTX_TIER1", TIER1_MODEL),
                             ("CTX_PLANNER", PLANNER_MODEL),
                             ("CTX_ADVISOR", ADVISOR_MODEL)) if m == TIER1_MODEL]
    if len(shared) < 2:
        return ""
    vals = {os.getenv(n) for n in shared if os.getenv(n)}
    if len(vals) > 1:
        return (f"⚠  {', '.join(shared)} are set to different values ({vals}) "
                f"but all name the same model ({TIER1_MODEL}) — only "
                f"MODEL_CTX[{TIER1_MODEL!r}] = {MODEL_CTX[TIER1_MODEL]} applies.")
    return ""


# ── Attempts per tier ────────────────────────────────────────────────────────
# Strikes before advancing a tier on the SAME repeated error.
PHASE_STRIKE_LIMIT = _i("PHASE_STRIKE_LIMIT", _BUDGET.get("strike_limit"), 2)

# Total validation failures allowed per tier, catching all-different thrashing.
# Tier 1 is cheap, so a task unsolved in 3 tries escalates rather than grinds.
# The last tier gets its own number so it can be tuned apart from the middle.
PHASE_MAX_ATTEMPTS = _i("PHASE_MAX_ATTEMPTS", _BUDGET.get("attempts_tier1"), 3)
TIER2_MAX_ATTEMPTS = _i("TIER2_MAX_ATTEMPTS", _BUDGET.get("attempts_mid"), 2)
TIER4_MAX_ATTEMPTS = _i("TIER4_MAX_ATTEMPTS", _BUDGET.get("attempts_last"), 2)

# ── Time budget ──────────────────────────────────────────────────────────────
BUDGET_SAMPLES    = _i("BUDGET_SAMPLES",    _BUDGET.get("samples"), 20)
BUDGET_MULTIPLIER = _f("BUDGET_MULTIPLIER", _BUDGET.get("multiplier"), 1.7)
BUDGET_FLOOR_S    = _i("BUDGET_FLOOR_S",    _BUDGET.get("floor_s"), 120)
BUDGET_CEILING_S  = _i("BUDGET_CEILING_S",  _BUDGET.get("ceiling_s"), 1200)
RETRY_BUDGET_MULT = _f("RETRY_BUDGET_MULT", _BUDGET.get("retry_mult"), 2.0)

# Absolute hard ceiling, checked at the top of EVERY attempt on every code path.
# The per-tier budgets above only apply in the "wrote code, validation failed"
# path and can be pushed out by the +300s bonus granted on each escalation.
# 2026-07-12: one task ran 25,769s because the bonuses compounded and the
# bad-pattern and no-output branches never consulted a budget at all.
MAX_TASK_SECONDS = _i("MAX_TASK_SECONDS", _BUDGET.get("task_ceiling_s"), 3600)

# Validation failures naming NO file this task wrote, before it is parked for a
# human rather than climbing the ladder. Cumulative, not consecutive — see
# work.py's park block.
FOREIGN_PARK_LIMIT = _i("FOREIGN_PARK_LIMIT", _BUDGET.get("foreign_park"), 2)


def describe() -> str:
    """One-line banner so a run states which ladder it is actually using."""
    src = f"profile '{PROFILE_NAME}'" if PROFILE_NAME else "defaults + env"
    tiers = " → ".join(dict.fromkeys(TIER_MODELS))
    return f"config: {src} | {tiers} | ceiling {MAX_TASK_SECONDS}s"
