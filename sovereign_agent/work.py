"""
work.py — Fully autonomous task execution loop.

After standup.py approves today's tasks, this script runs unattended:
  1. Reads every unchecked task for today from ROADMAP.md
  2. Uses Ollama 4B to identify which files are relevant
  3. Uses Ollama 35B to implement the changes (returns complete file contents)
  4. Runs flutter analyze / pytest to validate
  5. On failure:
     a. autofix.py applies deterministic mechanical fixes (zero API cost)
     b. qwen_advisor.py (qwen3.5:4b-nvfp4) classifies remaining errors,
        enriches context for the 35B, and drafts new .roorules entries
     c. 35B retries with enriched error context
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
TIER1_MODEL     = os.getenv("TIER1_MODEL",      "qwen2.5-coder:7b-instruct-q4_K_M")  # 7B — fast, strong on Dart
TIER2_MODEL     = os.getenv("TIER2_MODEL",      "qwen2.5-coder:32b")                  # 32B — second opinion
TIER_MODELS     = [TIER1_MODEL, TIER2_MODEL]   # Claude handles final escalation
RACE_MODEL      = os.getenv("RACE_MODEL",       "qwen2.5-coder:7b-instruct-q4_K_M")  # kept for future A/B experiments
RACE_ENABLED    = os.getenv("RACE_ENABLED", "0") == "1"   # off by default; enable with RACE_ENABLED=1
GIT_BRANCHES    = os.getenv("GIT_BRANCHES",  "1") == "1"  # branch-per-task; disable with GIT_BRANCHES=0
PLANNER_MODEL   = os.getenv("PLANNER_MODEL",    "qwen2.5-coder:7b-instruct-q4_K_M")
ADVISOR_MODEL   = os.getenv("ADVISOR_MODEL",    "qwen2.5-coder:7b-instruct-q4_K_M")

# Claude API escalation (final tier after all local models exhausted)
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_ENABLED      = os.getenv("ANTHROPIC_API_KEY", "") != ""  # auto-enabled when key present

# ── Per-model context windows ─────────────────────────────────────────────────
# Larger context = more files visible per task, but more VRAM for KV cache.
# Sized for M4 32 GB: small models get more context since their weights are cheaper.
# Override any entry via env vars (e.g. CTX_TIER1=16384).
MODEL_CTX: dict[str, int] = {
    TIER1_MODEL:   int(os.getenv("CTX_TIER1",   "32768")),  # 7B  ~4.7 GB weights → plenty of headroom
    TIER2_MODEL:   int(os.getenv("CTX_TIER2",   "16384")),  # 32B ~19 GB weights — tight, keep context small
    PLANNER_MODEL: int(os.getenv("CTX_PLANNER", "32768")),
    ADVISOR_MODEL: int(os.getenv("CTX_ADVISOR", "32768")),
}

# Strikes before advancing to the next tier (same error repeated)
PHASE_STRIKE_LIMIT = 1

# Hard cap on total validation failures per tier — catches thrashing (all-different errors)
PHASE_MAX_ATTEMPTS = 6

# ── Time budget ────────────────────────────────────────────────────────────────
# Max wall-clock seconds a single task may run before being skipped.
# Computed dynamically from rolling average of recently completed tasks.
BUDGET_SAMPLES    = 20    # recent completed tasks to include in average
BUDGET_MULTIPLIER = 1.7   # first pass: ~1.7× rolling average (6 min avg → 10 min budget)
BUDGET_FLOOR_S    = 120   # never cut off before 2 min regardless of average
BUDGET_CEILING_S  = 1200  # first-pass hard cap: 20 min
RETRY_BUDGET_MULT = 2.0   # retry pass ceiling: 2× first-pass ceiling = 40 min

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

def ensure_model_exists(model: str):
    """Check if model exists in Ollama; pull if missing."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags")
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            # Handle both exact matches and version-less matches if appropriate
            if model in models or f"{model}:latest" in models:
                return
        
        print(f"  {YELLOW}Model {model} not found locally. Pulling...{RESET}")
        with requests.post(f"{OLLAMA_URL}/api/pull", json={"name": model}, stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    body = json.loads(line)
                    if "status" in body:
                        status = body["status"]
                        if "completed" in body and "total" in body:
                            pct = (body["completed"] / body["total"]) * 100
                            print(f"\r  {DIM}Pulling {model}: {status} {pct:.1f}%{RESET}", end="", flush=True)
                        else:
                            print(f"\r  {DIM}Pulling {model}: {status}{RESET}", end="", flush=True)
        print(f"\n  {GREEN}✓ Model {model} pulled successfully.{RESET}")
    except Exception as e:
        print(f"\n  {RED}⚠ Failed to pull model {model}: {e}{RESET}")
        sys.exit(1)


_LARGE_MODEL_THRESHOLD_GB = 15  # flush before loading anything this large

# Rough VRAM footprint by model name fragment (GB).
# Used to decide whether to flush before loading a tier3/4 model.
_MODEL_SIZE_HINTS: dict[str, float] = {
    "32b":  19.0,
    "30b":  18.0,
    "r1":   19.0,
    "35b":  21.0,
    "24b":  14.0,
    "20b":  13.0,
    "16b":   8.9,
    "14b":   9.0,
    "7b":    4.7,
    "4b":    4.0,
}

def _model_size_gb(model_name: str) -> float:
    """Estimate model VRAM footprint from its name."""
    name = model_name.lower()
    for fragment, gb in _MODEL_SIZE_HINTS.items():
        if fragment in name:
            return gb
    return 10.0  # safe default if unknown


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
        print(f"  {DIM}Loading {size_gb:.0f}GB model ({model.split(':')[0]}) — keeping resident 10m...{RESET}")
        keep_alive = "10m"

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
    workers cannot corrupt each other's writes."""
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


# ─── File discovery ───────────────────────────────────────────────────────────

def all_source_files(project_root: str) -> list[str]:
    """Return all dart/py/js source files relative to project root."""
    exts = {".dart", ".py", ".js", ".ts"}
    skip = {"build", ".dart_tool", ".git", "node_modules", ".fvm"}
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

def read_files(paths: list[str], project_root: str, max_chars: int = 3000) -> dict[str, str]:
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

BAD_PATTERNS: list[tuple[str, str]] = [
    # Offset / Vector2 confusion in Flutter widget parameters
    (r'offset:\s*Vector2\b',
     "OFFSET ERROR: Transform.translate/Positioned offset: needs Flutter Offset(x,y), NOT Vector2(x,y). "
     "Flutter UI widgets use Offset; game objects use Vector2. Never pass Vector2 to a Flutter widget parameter."),

    # lengthSquared doesn't exist on Flame's Vector2
    (r'\.lengthSquared\b',
     "API ERROR: Vector2 has no .lengthSquared. Use .length, or compute (v.x*v.x + v.y*v.y) manually."),

    # Wrong Star constructor (factionId is a getter, not a named constructor)
    (r'Star\.factionId\s*\(',
     "CONSTRUCTOR ERROR: Star.factionId is a getter, NOT a constructor. "
     "Use Star.forFaction(factionId, position: Vector2(x,y)) instead."),

    # Flame Effect API (broken in this codebase — use plain Dart timers)
    (r"import.*flame/effects\.dart",
     "IMPORT ERROR: Do NOT import flame/effects.dart. Use plain Dart timer classes for animation "
     "(see _FusionAnimation in astro_game.dart for the pattern)."),
    (r'\bMoveEffect\b|\bRotateEffect\b|\bScaleEffect\b|\bOpacityEffect\b',
     "FLAME API ERROR: Don't use Flame Effect classes. Use plain Dart timer pattern instead."),

    # Canvas draw calls with Vector2 instead of Offset.
    # Allow: Offset(...), anyOffset, start.toOffset() — block bare Vector2 identifiers.
    # Old regex [^O][^f] was too broad: it also fired on correct code like
    # canvas.drawLine(start.toOffset(), ...) and canvas.drawLine(startOffset, ...).
    (r'canvas\.drawLine\s*\(\s*(?!\w*[Oo]ffset)(?!\w+\.toOffset\b)[A-Za-z_]',
     "CANVAS ERROR: canvas.drawLine() takes Offset args, not Vector2. "
     "Store fields as Vector2; convert only at the render call site: "
     "canvas.drawLine(start.toOffset(), end.toOffset(), paint)  "
     "or canvas.drawLine(Offset(start.x, start.y), Offset(end.x, end.y), paint)."),
    (r'canvas\.drawCircle\s*\(\s*\w+\.position\b',
     "CANVAS ERROR: canvas.drawCircle() takes Offset, not Vector2. "
     "Wrap: Offset(pos.x, pos.y)."),

    # Unqualified generateId
    (r'(?<!\.)generateId\(\)',
     "STATIC METHOD ERROR: generateId() must be qualified as BaseUnit.generateId(). "
     "Dart does not inherit static members."),

    # .normalized() — unreliable on older vector_math
    (r'\.normalized\(\)',
     "API ERROR: Avoid .normalized() — use (diff / distance) pattern for safe normalization."),

    # endPosition in _AnimatedMote or similar internal classes omitted
    (r'required this\.endPosition',
     "CONSTRUCTOR MISMATCH: If endPosition is required, every call site must pass it. "
     "Consider making it optional or removing it."),

    # flame/game.dart without hide Vector → ambiguous_import (vector_math also exports Vector)
    (r"import 'package:flame/game\.dart'(?! hide Vector)",
     "IMPORT ERROR: flame/game.dart re-exports vector_math's Vector type. "
     "ALWAYS write: import 'package:flame/game.dart' hide Vector; "
     "Omitting 'hide Vector' causes ambiguous_import at every use of our game unit Vector."),

    # super.update(dt) inside a Flame Game subclass — Game.update is abstract
    (r'super\.update\(dt\)',
     "FLAME API ERROR: Game.update(double dt) is abstract — NEVER call super.update(dt). "
     "Just override it: @override void update(double dt) { /* your logic */ }"),

    # .notifier() called as a function — StateNotifierProvider.notifier is a getter, not a method
    (r'\.notifier\s*\(\s*\)',
     "RIVERPOD ERROR: .notifier is a getter on StateNotifierProvider, NOT a method. "
     "Never call gameServiceProvider.notifier() with parentheses. "
     "Correct usage: container.read(gameServiceProvider.notifier) — no parentheses."),

    # fuseWith is game logic — must never be added to pure data models (Mote, Vector, Star)
    (r'def fuseWith|void fuseWith|Vector\? fuseWith|fuseWith\(',
     "MODEL PURITY ERROR: fuseWith() is game logic and must NOT be added to Mote, Vector, or Star. "
     "These are pure data classes (id + toMap/fromMap only). "
     "Fusion logic belongs in lib/game/ or lib/systems/, not in models/."),

    # AudioPlayerEffect is an invented class
    (r'\bAudioPlayerEffect\b',
     "INVENTED CLASS ERROR: AudioPlayerEffect does not exist in flame_audio. "
     "Use FlameAudio.play(filename) for one-shot effects. No wrapper class needed."),

    # Bgm is not a public type — access via FlameAudio.bgm, never instantiate or type-annotate
    (r'\bBgm\b',
     "FLAME AUDIO ERROR: 'Bgm' is not a public type you can import or annotate. "
     "Access the background music player via FlameAudio.bgm (no import needed). "
     "Never write 'Bgm player' or 'late Bgm _bgm' — just call FlameAudio.bgm.play(...) directly."),

    # FlameAudio.bgm.isPlaying is a getter, not a method
    (r'FlameAudio\.bgm\.isPlaying\s*\(',
     "FLAME AUDIO ERROR: FlameAudio.bgm.isPlaying is a bool getter, not a method. "
     "Use 'if (FlameAudio.bgm.isPlaying)' not 'if (FlameAudio.bgm.isPlaying())'"),

    # withOpacity is deprecated in Flutter 3.x — use withValues(alpha:)
    (r'\.withOpacity\(',
     "DEPRECATION ERROR: .withOpacity() is deprecated. "
     "Use .withValues(alpha: x) instead."),

    # Colors.magenta does not exist in Flutter — use Colors.purple
    (r'Colors\.magenta\b',
     "COLOR ERROR: Colors.magenta does not exist in Flutter. "
     "Use Colors.purple (enemy faction color throughout this codebase)."),

    # const Vector2(...) — Vector2 is not const-constructable (backed by Float64List)
    (r'const\s+Vector2\s*\(',
     "CONST ERROR: Vector2 is NOT const-constructable. "
     "Remove 'const': use Vector2(x, y) not const Vector2(x, y)."),

    # Non-const instance field in a class that has a const constructor
    # e.g. class Foo { const Foo(); final List<...> items = [Bar()]; }  ← Bar() not const
    (r'const\s+\w+\(\{[^}]*super\.key[^}]*\}\)[\s\S]{0,400}final\s+\w+[^=]+=\s*\[',
     "CONST CONSTRUCTOR ERROR: A class with 'const MyWidget({super.key})' cannot have "
     "non-const field initializers (e.g. 'final list = [SomeClass()]'). "
     "Move the field initializer into the build() method as a local variable instead."),

    # Hallucinated / typo'd package names — models invent these under pressure
    (r"flutter_riverpod/flutter_river(?!pod\.dart)",
     "IMPORT TYPO: The correct import is 'package:flutter_riverpod/flutter_riverpod.dart'. "
     "Do NOT use flutter_riverpad, flutter_riverpost, or any other variant."),

    (r"package:flame/(?:math_engine|geometry_engine|physics_engine|vector_engine|render_engine)\b",
     "HALLUCINATED PACKAGE: flame/math_engine.dart (and similar) do not exist. "
     "Use package:flame/components.dart for Vector2/Component, dart:math for math utilities."),

    # Swift/Kotlin-style optional cast — not valid Dart
    (r'\bas\?\s+\w',
     "SYNTAX ERROR: Dart does not have 'as?' optional casting (that's Swift/Kotlin). "
     "Use 'if (x is MyType)' or 'x is MyType ? x as MyType : null' instead."),

    # Offset used as start/end for AttackLineComponent — which expects Vector2
    (r'start:\s*Offset\s*\(|end:\s*Offset\s*\(',
     "TYPE ERROR: AttackLineComponent.start and .end are Vector2, not Offset. "
     "Use position.clone() or Vector2(x, y) — never Offset(x, y) for Flame component fields."),
]

# ─── Error → targeted hint mapping ───────────────────────────────────────────
# When flutter analyze output matches a pattern, the hint is prepended to the
# errors fed back to the 35B on the next retry, giving it precise guidance.
ERROR_HINTS: list[tuple[str, str]] = [
    (r"can.t be assigned to the parameter type 'int'|argument_type_not_assignable.*\bint\b",
     "⚠️  STRING passed where INT expected. All model IDs are int, never String:\n"
     "  Star.id → int,  Vector.id → int,  Star.ownerId → String (player label only).\n"
     "  CaptureRules.tryPerformCapture(notifier, vectorId: int, starId: int) — no player string.\n"
     "  CombatAttackEvent(sourceVectorId: int, targetStarId: int) — both int.\n"
     "  Never pass ownerId/playerOwnerId where an id parameter is expected."),

    (r'argument_type_not_assignable.*Offset|can.t be assigned.*Offset',
     "⚠️  OFFSET vs VECTOR2: Flutter widget parameters (offset:, position:, etc.) "
     "require Offset(x, y) — NEVER pass Vector2. "
     "Offset has .dx/.dy; Vector2 has .x/.y. They are incompatible types."),

    (r'undefined_getter.*lengthSquared|undefined_method.*lengthSquared',
     "⚠️  Vector2 has NO .lengthSquared — use .length or compute v.x*v.x + v.y*v.y manually."),

    (r'library_private_types_in_public_api',
     "⚠️  Private types (names starting with _) cannot appear in public class fields/methods. "
     "Either rename the private type to be public, or make the containing class private (_ClassName)."),

    (r'undefined_function.*Vector2|undefined_method.*Vector2',
     "⚠️  Vector2 is not imported. Add: import 'package:flame/components.dart' hide Vector;"),

    (r'missing_required_argument',
     "⚠️  A required constructor parameter is missing. Read the class definition carefully "
     "and provide ALL required: named parameters."),

    (r'extra_positional_arguments|2 positional arguments expected by .AsyncError|'
     r'2 positional arguments expected by .error',
     "⚠️  AsyncError requires TWO positional arguments: AsyncError(error, stackTrace). "
     "Never call AsyncError(e) with one argument — Dart requires the stack trace too. "
     "Pattern: catch (e, st) { return AsyncValue.error(e, st); } "
     "or AsyncError(e, StackTrace.current) if outside a catch block."),

    (r'extends_non_class|non_type_as_type_argument',
     "⚠️  extends_non_class: you are extending something that is not a class. "
     "Common Riverpod mistakes: do NOT write 'extends AsyncNotifier' without the generic "
     "type parameter, do NOT extend a provider (e.g. 'extends FusionProvider'), "
     "and do NOT extend abstract classes that require type args without providing them. "
     "Correct patterns: 'class X extends StateNotifier<MyState>', "
     "'class X extends AsyncNotifier<MyType>'."),

    (r'undefined_method.*distanceTo|Offset.*distanceTo',
     "⚠️  Offset has no distanceTo(). Use (o - Offset(v.x, v.y)).distance instead."),

    (r'undefined_method.*normalized\b',
     "⚠️  .normalized() is unreliable — use (diff / distance) pattern: "
     "final n = diff / diff.length;"),

    (r'argument_type_not_assignable.*List<Nova>|argument_type_not_assignable.*List<Vector>',
     "⚠️  Type mismatch: use typed lists <Nova>[], <Vector>[] — not <dynamic>[]."),

    (r'ambiguous_import.*Vector|Vector.*ambiguous_import',
     "⚠️  ambiguous_import for 'Vector': flame/game.dart re-exports vector_math's Vector. "
     "Fix: import 'package:flame/game.dart' hide Vector;"),

    (r'abstract_super_member_reference.*update|update.*abstract_super_member_reference',
     "⚠️  Game.update(double dt) is abstract — remove super.update(dt) from your override. "
     "Just call your own logic directly."),

    (r'deprecated_member_use.*withOpacity|withOpacity.*deprecated',
     "⚠️  .withOpacity() is deprecated — use .withValues(alpha: x) instead."),

    (r'const_with_non_const',
     "⚠️  const_with_non_const: a widget class has 'const' constructor but a non-const "
     "field initializer (e.g. 'final list = [SomeClass()]'). "
     "Fix: move the field into build() as a local variable, NOT an instance field."),

    (r'creation_with_non_type.*Vector2|Vector2.*creation_with_non_type',
     "⚠️  Vector2 is not in scope. Add: import 'package:flame/components.dart'; "
     "(use 'hide Vector' if also importing models/vector.dart). "
     "Also: NEVER use 'const Vector2(...)' — Vector2 is not const-constructable."),

    (r'annotate_overrides',
     "⚠️  annotate_overrides: a method/getter overrides a parent but is missing '@override'. "
     "Add @override on the line immediately before the method/getter declaration."),

    (r"isn't defined.*Level1|Level1.*isn't defined",
     "⚠️  Level API: there is NO 'Level1' class. The correct class is 'Level001' in "
     "lib/levels/level_001.dart. Its definition is a static getter: Level001.definition "
     "which returns a LevelDef — NOT stars/motes/vectors directly. "
     "For tests, construct Star objects manually using Faction.player / Faction.enemy, "
     "call game.initialize([star], [], [], []), set game.state.gameState = 'playing', "
     "then call game.update(1.0) in a loop."),

    (r'uri_does_not_exist',
     "⚠️  uri_does_not_exist: a package import cannot be resolved. "
     "This means the package is missing from pubspec.yaml OR flutter pub get has not been run. "
     "Do NOT remove the import — the package is already in pubspec.yaml. "
     "This error will clear on its own once 'flutter pub get' is run. "
     "Rewrite the file with the same imports unchanged."),

    (r"positional argument.*GameStateNotifier|GameStateNotifier.*positional argument",
     "⚠️  GameStateNotifier requires ONE positional argument: a PersistenceService. "
     "Correct pattern:\n"
     "  import 'package:astro_flux/systems/local_persistence_service.dart';\n"
     "  final notifier = GameStateNotifier(LocalPersistenceService());\n"
     "NEVER call GameStateNotifier() with no arguments — it will not compile."),

    (r"isn't defined for the type 'StarCaptureIndicator'|"
     r"_progress.*StarCaptureIndicator|_captured.*StarCaptureIndicator",
     "⚠️  StarCaptureIndicator private fields (_progress, _captured) cannot be accessed "
     "directly from tests. Use the public @visibleForTesting getters instead:\n"
     "  indicator.progress   (double, 0.0–1.0)\n"
     "  indicator.captured   (bool)\n"
     "Do NOT access _progress or _captured directly."),

    (r"components/stars/star_capture_indicator|"
     r"Target of URI doesn't exist.*star_capture_indicator",
     "⚠️  StarCaptureIndicator import path: the file lives at "
     "lib/components/star_capture_indicator.dart. "
     "Import it as: import 'package:astro_flux/components/star_capture_indicator.dart';\n"
     "The path components/stars/star_capture_indicator.dart is a re-export alias — "
     "prefer the canonical path in new code."),

    (r"Target of URI doesn't exist.*level_up_event_bus|"
     r"level_up_event_bus.*doesn't exist",
     "⚠️  level_up_event_bus.dart does not exist in this project. "
     "Remove any import of 'package:astro_flux/game/level_up_event_bus.dart' — "
     "it is an unused import generated in error. Do not create the file."),

    (r"part of.*astro_flux\.|part of.*library",
     "⚠️  Do NOT use 'part of' directives. This project does not use Dart part files. "
     "Every .dart file must be a standalone library. "
     "Remove any 'part of <library>;' line at the top of the file."),

    (r"game_core\.dart|import.*game_core|GameCore.*isn't a function|isn't a function.*GameCore",
     "⚠️  GameCore is NOT a class you can instantiate. Do not call GameCore(...). "
     "The correct pattern for embedding the game in Flutter is Flame's built-in GameWidget:\n"
     "  import 'package:flame/game.dart';\n"
     "  import 'package:astro_flux/game/astro_game.dart';\n"
     "  GameWidget<AstroGame>(\n"
     "    game: AstroGame(),\n"
     "    overlayBuilderMap: {'gameOver': (ctx, game) => GameOverOverlay()},\n"
     "  )\n"
     "Never use GameCore(...) — use GameWidget<AstroGame>(...) instead."),

    (r"game_rules_engine\.dart|import.*game_rules_engine",
     "⚠️  game_rules_engine.dart does not contain game logic — it is a re-export stub. "
     "Game rules live in: lib/game/capture_rules.dart and lib/game/fusion_rules.dart. "
     "Import those directly instead."),

    (r"StateNotifier.*not found|StateNotifierProvider.*not found|"
     r"Type 'StateNotifier' not found|Method not found: 'StateNotifierProvider'",
     "⚠️  RIVERPOD 3.x: StateNotifier and StateNotifierProvider are REMOVED.\n"
     "  Migrate to Notifier<T> + NotifierProvider:\n"
     "  OLD (broken):\n"
     "    class MyNotifier extends StateNotifier<MyState> {\n"
     "      MyNotifier() : super(MyState.initial);\n"
     "      void doSomething() { state = newState; }\n"
     "    }\n"
     "    final myProvider = StateNotifierProvider<MyNotifier, MyState>((ref) => MyNotifier());\n"
     "  NEW (correct):\n"
     "    class MyNotifier extends Notifier<MyState> {\n"
     "      @override\n"
     "      MyState build() => MyState.initial;  // replaces super(initialState)\n"
     "      void doSomething() { state = newState; }  // state getter/setter unchanged\n"
     "    }\n"
     "    final myProvider = NotifierProvider<MyNotifier, MyState>(() => MyNotifier());\n"
     "  ref.read(myProvider.notifier) still works. ref.watch(myProvider) still works.\n"
     "  WidgetRef and Ref are SEPARATE types in Riverpod 3.x — do not pass WidgetRef as Ref."),

    (r"argument type 'WidgetRef' can't be assigned to.*'Ref'|"
     r"WidgetRef.*can't be assigned.*Ref",
     "⚠️  RIVERPOD 3.x: WidgetRef and Ref are SEPARATE types — cannot pass WidgetRef as Ref.\n"
     "  To give a long-lived object (e.g. AstroGame) a proper Ref, create it inside a Provider:\n"
     "    final myGameProvider = Provider<AstroGame>((ref) => AstroGame(ref));\n"
     "  Then read it in initState:\n"
     "    _game = ref.read(myGameProvider);  // ref here is WidgetRef, but the game gets Ref\n"
     "  Never pass the WidgetRef from ConsumerState.ref directly to a Ref parameter."),

    (r"Classes can only extend other classes|extends.*Provider|extends.*Notifier(?!<)",
     "⚠️  A class is trying to extend a provider or non-class type. "
     "Riverpod providers are not classes you extend. Correct patterns:\n"
     "  class MyNotifier extends Notifier<MyState> { ... }  // Riverpod 3.x\n"
     "  final myProvider = NotifierProvider<MyNotifier, MyState>(() => MyNotifier());\n"
     "Never write 'extends fusionProvider' or 'extends NotifierProvider'."),

    (r"valueOrNull.*ConnectivityResult|ConnectivityResult.*valueOrNull",
     "⚠️  AsyncValue<List<ConnectivityResult>> does not have valueOrNull in this Riverpod version. "
     "Use .when() or .value instead:\n"
     "  final result = ref.watch(connectivityProvider);\n"
     "  final isOnline = result.value?.contains(ConnectivityResult.wifi) ?? false;\n"
     "Or use connectivityProvider as a plain Provider<ConnectivityResult> if async is not needed."),

    (r"game_events\.dart|VectorFusedEvent|LevelUpEventBus",
     "⚠️  game_events.dart, VectorFusedEvent, and LevelUpEventBus do not exist in this project. "
     "Do not import or reference them. For fusion events use canFuseProvider from "
     "lib/game/fusion_provider.dart which reads mote count directly from gameServiceProvider."),

    (r"toVector2.*isn't defined.*Vector2|Vector2.*toVector2.*isn't defined",
     "⚠️  .toVector2() DOES NOT EXIST ON Vector2 — it's already a Vector2.\n"
     "  In Flame's ScaleUpdateInfo:\n"
     "    info.delta.global  → Vector2 (already)  — use it directly\n"
     "    info.scale.global  → Vector2 (already)  — use .x or .y directly\n"
     "  Only call .toVector2() on Offset or other non-Vector2 types.\n"
     "  CORRECT: camera.viewfinder.position -= info.delta.global / zoom;\n"
     "  WRONG:   camera.viewfinder.position -= info.delta.global.toVector2() / zoom;"),

    (r"ScaleDetector.*can't be mixed|can't be mixed.*ScaleDetector"
     r"|Classes can only mix in mixins.*gesture|Classes can only extend.*GestureHandler",
     "⚠️  FLAME GESTURE MIXIN ERROR: ScaleDetector, TapDetector, DragCallbacks etc. can only be\n"
     "  mixed onto FlameGame (or a Component that satisfies their 'on' constraint).\n"
     "  NEVER create 'class GestureHandler extends PositionComponent with ScaleDetector' — invalid.\n"
     "  CORRECT pattern — put gesture mixins on AstroGame (already done, file is LOCKED):\n"
     "    class AstroGame extends FlameGame with ScaleDetector {\n"
     "      @override void onScaleStart(ScaleStartInfo info) { ... }\n"
     "      @override void onScaleUpdate(ScaleUpdateInfo info) {\n"
     "        if (info.pointerCount >= 2) {\n"
     "          camera.viewfinder.zoom = newZoom.clamp(0.4, 2.0);\n"
     "          camera.viewfinder.position -= delta / camera.viewfinder.zoom;\n"
     "        }\n"
     "      }\n"
     "    }\n"
     "  AstroGame already has ScaleDetector wired — do NOT rewrite it.\n"
     "  For tap handling on individual components use 'with TapCallbacks' on the COMPONENT."),

    (r"CombatResultLabel.*isn't defined|capture_result_label|combat_result_label\.dart",
     "⚠️  Do NOT create a new capture/combat label component or file. The infrastructure already exists:\n"
     "  • lib/game/capture_event_notifier.dart — CaptureMessageNotifier (StateNotifier<String>)\n"
     "  • captureMessageProvider — Provider<String> you can watch with Consumer or ref.watch()\n"
     "  To display the message, add a Consumer widget that watches captureMessageProvider and\n"
     "  shows an Overlay or AnimatedSwitcher. Do NOT create capture_result_label_component.dart\n"
     "  or combat_result_label.dart — those files do not exist and should not be created.\n"
     "  The locked CombatResultLabelComponent at lib/components/combat_result_label_component.dart\n"
     "  is a Flame PositionComponent for in-world labels — use CaptureMessageNotifier for HUD toasts."),

    (r"firebase_remote_config|firebase_core|cloud_firestore|firebase_auth",
     "⚠️  Firebase packages are NOT in this project. Do not import any firebase_* package.\n"
     "  This project uses LocalPersistenceService (in-memory) for persistence — no Firebase.\n"
     "  Remove all firebase_remote_config, firebase_core, cloud_firestore imports immediately."),

    (r"flutter_vector_math|package:vector_math/vector_math_64|vector_math\.dart",
     "⚠️  Do NOT import flutter_vector_math or vector_math directly.\n"
     "  Vector2 comes from Flame: 'import package:flame/components.dart' (or package:flame/game.dart).\n"
     "  Both already re-export vector_math's Vector2. Never add a separate vector_math import."),

    (r"Directives must appear before any declarations|directive.*before.*declaration",
     "⚠️  IMPORT AFTER CLASS: All 'import' statements must appear at the TOP of the file,\n"
     "  before any class, enum, or function declarations.\n"
     "  Move every import to lines 1-N before the first 'class' or 'enum' keyword."),

    (r"Target of URI doesn't exist.*'../game_state_provider|'../game_state_provider",
     "⚠️  WRONG IMPORT PATH for game_state_provider.dart.\n"
     "  The file is at lib/models/game_state_provider.dart.\n"
     "  From lib/game/*.dart:    import '../models/game_state_provider.dart';\n"
     "  From lib/game/ai/*.dart: import '../../models/game_state_provider.dart';\n"
     "  ALWAYS SAFE: import 'package:astro_flux/models/game_state_provider.dart';\n"
     "  NEVER write '../game_state_provider.dart' — there is no game_state_provider in lib/game/."),

    (r"'Mote' isn't a function|Mote.*isn't a function",
     "⚠️  'Mote' IS A CLASS, not a function. Never call it positionally.\n"
     "  Correct constructor: Mote(id: someInt)\n"
     "  Mote has NO position field — it only has: id (int), lifecycleState (MoteLifecycle).\n"
     "  To check if active: mote.isActive  (getter, not a method call)\n"
     "  Do NOT write: Mote(id, position), Mote(id), or state.motes.map(Mote).\n"
     "  Lifecycle helpers return new instances: mote.setActive(), mote.setFused(), mote.setCreated()"),

    (r"'text' can't be used as a setter.*final|final.*'text'.*setter",
     "⚠️  CombatResultLabelComponent.text is final — you cannot mutate it after construction.\n"
     "  To show a new label, remove the old component and add a new one:\n"
     "    parent.remove(oldLabel);\n"
     "    parent.add(CombatResultLabelComponent(text: 'Captured!', color: Colors.green));\n"
     "  Do NOT write: label.text = 'something'; — that will always fail with a setter error."),

    (r"Classes can only extend other classes.*capture|capture.*Classes can only extend",
     "⚠️  CaptureEventNotifier / CaptureMessageNotifier must extend StateNotifier<String>, not a provider.\n"
     "  CORRECT:\n"
     "    class CaptureMessageNotifier extends StateNotifier<String> {\n"
     "      CaptureMessageNotifier() : super('');\n"
     "    }\n"
     "  WRONG: extends captureMessageProvider, extends StateNotifierProvider, extends Provider.\n"
     "  Providers are instances created by the framework — you never extend them."),

    (r"isn't a valid override of.*PositionComponent|CombatResultLabelComponent.*position.*isn't",
     "⚠️  Do NOT declare 'final Vector2 position' as an instance field in a PositionComponent subclass. "
     "PositionComponent already has a 'position' property — re-declaring it causes an override conflict. "
     "Instead, accept the initial position as a constructor parameter named 'initialPosition' and pass "
     "it only to super():\n"
     "  class MyComponent extends PositionComponent {\n"
     "    MyComponent({Vector2? initialPosition}) : super(position: initialPosition ?? Vector2.zero());\n"
     "    // Use 'position' (inherited) directly — never redeclare it as a field.\n"
     "  }"),

    (r"Target of URI doesn't exist.*audio_service|audio_service.*Target of URI"
     r"|'../../audio_service\.dart'|'../audio_service\.dart'",
     "⚠️  WRONG IMPORT PATH for audio_service.dart. The canonical file is at lib/services/audio_service.dart.\n"
     "  From lib/game/ai/*.dart use:      import '../../services/audio_service.dart';\n"
     "  From lib/game/*.dart use:         import '../services/audio_service.dart';\n"
     "  From lib/game_ui/*.dart use:      import '../services/audio_service.dart';\n"
     "  From lib/components/*.dart use:   import '../services/audio_service.dart';\n"
     "  ALWAYS SAFE: import 'package:astro_flux/services/audio_service.dart';\n"
     "  NEVER write: import '../audio_service.dart' or import '../../audio_service.dart' — "
     "audio_service.dart lives in services/, not in game/ or game/ai/."),

    (r"Target of URI doesn't exist.*capture_rules|capture_rules.*Target of URI",
     "⚠️  WRONG IMPORT PATH for capture_rules.dart. The canonical file is at lib/game/capture_rules.dart.\n"
     "  From lib/game/ai/*.dart use:      import '../capture_rules.dart';\n"
     "  From lib/game_ui/*.dart use:      import '../game/capture_rules.dart';\n"
     "  From lib/components/*.dart use:   import '../game/capture_rules.dart';\n"
     "  Or always safe: import 'package:astro_flux/game/capture_rules.dart';"),

    (r"body might complete normally.*'null'.*return type.*'bool'|non_nullable_return_type",
     "⚠️  METHOD MISSING RETURN STATEMENT: A method declared to return 'bool' (or another non-nullable type) "
     "has no return statement — the body completes without returning a value. Add an explicit return:\n"
     "  static bool canAttack(...) {\n"
     "    if (someCondition) return false;\n"
     "    return true;  // ← must always return\n"
     "  }"),

    (r"package:particle_effects|Target of URI doesn't exist.*particle_effects\.dart'(?!.*astro_flux)",
     "⚠️  'package:particle_effects' does NOT exist — it is a hallucinated external package.\n"
     "  The particle system is internal to this project:\n"
     "    import 'package:astro_flux/game/particle_effects.dart';   // ParticleEffects, CombatEffect\n"
     "    import 'package:astro_flux/game/particle_system.dart';    // ParticleSystem\n"
     "  API: ParticleEffects(ParticleSystem system).trigger(CombatEffect, Vector2, Color)\n"
     "  CombatEffect values: attack, hit, destroy, heal\n"
     "  NEVER import from 'package:particle_effects/...' — that package is not in pubspec.yaml."),

    (r"'CombatAttackEvent' isn't a function|CombatAttackEvent.*isn't a function",
     "⚠️  CombatAttackEvent IS a class — use its named constructor, never call it positionally.\n"
     "  Correct: CombatAttackEvent(sourceVectorId: 1, targetStarId: 2)\n"
     "  Import:  import 'package:astro_flux/game/combat_attack_event.dart';\n"
     "  NEVER write: CombatAttackEvent(1, 2) — both parameters are named and required.\n"
     "  The class has exactly two fields: sourceVectorId (int) and targetStarId (int)."),

    (r"getter 'state' isn't defined.*GameStateNotifier|"
     r"'state'.*isn't defined.*type 'GameStateNotifier'|"
     r"_gameStateNotifier\.state",
     "⚠️  GameStateNotifier does NOT expose a public 'state' getter — never access .state on it.\n"
     "  To READ game state:    final state = ref.read(gameServiceProvider);\n"
     "  To MUTATE game state:  ref.read(gameServiceProvider.notifier).someMethod();\n"
     "  CombatAttackHandler and UnitCombatResolver both take a Ref, not a GameStateNotifier:\n"
     "    class CombatAttackHandler { final Ref _ref; CombatAttackHandler(this._ref); }\n"
     "  NEVER write: notifier.state  or  _gameStateNotifier.state"),
]


def apply_error_hints(error_output: str) -> str:
    """Prepend targeted hints for any recognised error patterns."""
    hints = []
    for pattern, hint in ERROR_HINTS:
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
        if _re.search(pattern, content):
            violations.append(f"[{rel_path}] {msg}")
    return violations


def implement_task(task: str, file_contents: dict[str, str], errors: str = "",
                   model: str | None = None,
                   is_test: bool = False,
                   project_root: str = ".") -> tuple[dict[str, str], dict]:
    """Ask Ollama to implement the task. Returns {rel_path: new_content}."""
    model = model or EXECUTOR_MODEL
    # Only send editable files as context — locked files waste tokens and confuse the model
    editable = {p: c for p, c in file_contents.items() if p not in LOCKED_FILES}
    context = "\n\n".join(
        f"=== {path} ===\n{content}"
        for path, content in editable.items()
    )
    vision = open("VISION.md").read() if os.path.exists("VISION.md") else ""
    rules  = open(".roorules").read()  if os.path.exists(".roorules")  else ""

    # Inject API guide so the model knows what already exists before inventing new classes.
    # Skip for T4 models (12 288-token context): the guide alone costs ~5 000 tokens
    # and leaves too little room for file context.  T4 is the most capable model and
    # least likely to hallucinate APIs; it gets the pitfalls block instead.
    _api_guide_path = os.path.join(project_root, "docs", "API_GUIDE.md")
    api_guide_block = ""
    _is_t4 = model == TIER1_MODEL
    if os.path.exists(_api_guide_path) and not _is_t4:
        _guide = open(_api_guide_path).read()
        # T3 gets a trimmed guide (first 6 000 chars ≈ 2 000 tokens) to save headroom.
        _is_t3 = model == TIER3_MODEL
        _guide = _guide[:6000] if _is_t3 else _guide
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
        "You are a Dart/Flutter coding agent. Implement the given task.\n"
        "Return ONLY a JSON object where:\n"
        "  - Keys are file paths exactly as given (e.g. 'lib/game/game_core.dart')\n"
        "  - Values are the COMPLETE new file contents (not diffs, not snippets)\n"
        "Only include files that DIRECTLY change to implement this specific task.\n"
        "DO NOT touch files that are not required. DO NOT refactor or improve unrelated files.\n"
        "DO NOT create new files unless the task explicitly says to create one.\n"
        "Typical task requires 1-3 files. If you find yourself changing more than 5, stop and reconsider.\n"
        "Keep implementations MINIMAL — write the fewest lines that make the task work. "
        "Do not add elaborate systems, helpers, or abstractions unless required.\n"
        "No explanation or markdown outside the JSON object.\n\n"
        "CRITICAL — these files are LOCKED and will be silently ignored if returned:\n"
        f"{locked_list}\n\n"
        "Coding rules:\n"
        f"{rules}"
        f"{pitfalls_block}"
        f"{scope_block}"
    )
    user_prompt = (
        f"Task: {task}\n\n"
        f"Project vision summary:\n{vision[:1500]}\n"
        f"{api_guide_block}\n\n"
        f"Current file contents:\n{context}"
        f"{error_block}"
    )
    # Scale timeout with model size: large 32B models need 15+ min on a Mac Air
    # (3-4 min to load + 10-12 min to generate a full file).
    size_gb = _model_size_gb(model)
    coding_timeout = 900 if size_gb >= _LARGE_MODEL_THRESHOLD_GB else 600
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
                  test_only: bool = False) -> tuple[list[str], str]:
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
        if rel_path in LOCKED_FILES:
            print(f"  {DIM}(skipped locked file: {rel_path}){RESET}")
            continue
        # Scope guard: test tasks must only write under test/
        if test_only and not rel_path.startswith("test/"):
            msg = (
                f"SCOPE VIOLATION [{rel_path}]: This is a 'Write unit test' task — "
                f"only files under test/ are permitted. '{rel_path}' is a source file "
                f"and must NOT be modified here. "
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
        violations = check_bad_patterns(rel_path, content) if pathlib.Path(rel_path).suffix == '.dart' else []
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
    # Always start from main so branches don't stack on each other
    _git(["checkout", "main"], project_root)
    r = _git(["checkout", "-b", branch], project_root)
    if r.returncode != 0:
        # Branch exists from a previous aborted run — reuse it
        _git(["checkout", branch], project_root)
    return branch


def _branch_merge(project_root: str, branch: str, task_idx: int, task: str) -> None:
    """Commit the task changes, merge to main, delete the branch."""
    _git(["add", "-A"], project_root)
    _git(["commit", "-m", f"task {task_idx}: {task[:72]}"], project_root)
    _git(["checkout", "main"], project_root)
    _git(["merge", "--no-ff", branch, "-m", f"Merge task-{task_idx}"], project_root)
    _git(["branch", "-D", branch], project_root)


def _branch_abandon(project_root: str, branch: str) -> None:
    """Discard a failed task branch and return to main."""
    _git(["checkout", "main"], project_root)
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

    additional_bad_patterns : list[{"pattern": str, "hint": str}]
        Extra entries appended to BAD_PATTERNS after the built-in list.

    additional_error_hints : list[{"pattern": str, "hint": str}]
        Extra entries appended to ERROR_HINTS after the built-in list.

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
                        doc_id = code.replace("/", "_")[:128]
                        col.document(doc_id).set({
                            "code":      code,
                            "count":     rec.get("count", 1),
                            "last_task": rec.get("task", "")[:120],
                            "last_date": rec.get("date", date.today().isoformat()),
                            "hint":      rec.get("hint", ""),
                            "source":    "local",
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
                        doc_id = _hl2.md5(rule_text.encode()).hexdigest()[:16]
                        col.document(doc_id).set({
                            "rule":      rule_text,
                            "task_idx":  rec.get("task_idx", 0),
                            "date":      rec.get("date", date.today().isoformat()),
                            "source":    "local",
                            "applied":   False,
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
                    doc_id = _hl2.md5(key.encode()).hexdigest()[:16]
                    col.document(doc_id).set(rec, merge=True)
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
        for doc in db.collection("error_patterns").stream():
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
        for doc in db.collection("project_rules").stream():
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


def validate(baseline_errors: frozenset[str] | None = None) -> tuple[bool, str]:
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
            # Flutter exits with code 1 even for plugin deprecation warnings
            # (e.g. "This will become an error in a future version of Flutter").
            # We only fail if NEW error • lines appear that weren't in the
            # pre-task baseline — pre-existing errors the model didn't cause
            # must not block the task.
            current_errors = _extract_flutter_errors(output)
            new_errors = current_errors - (baseline_errors or frozenset())
            if not new_errors:
                return True, output[-3000:]
            # Build an output that highlights only the new errors
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
    rules  = open(".roorules").read()  if os.path.exists(".roorules")  else ""
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
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start == -1:
            print(f" {YELLOW}no JSON in response{RESET}")
            return {}, False
        changes = json.loads(raw[start:end])
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
    rules  = open(".roorules").read()  if os.path.exists(".roorules")  else ""
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
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start == -1:
            print(f" {YELLOW}no JSON in response{RESET}")
            return {}, False
        parsed = json.loads(raw[start:end])
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
             start_tier_idx: int = 0) -> bool | str:
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
    phase_stale: int    = 0   # same-error streak within current tier
    phase_attempts: int = 0   # total validation failures in current tier (catches thrashing)
    last_err_sig   = ""

    is_test = _is_test_task(task)
    call_info: dict = {}    # populated by implement_task on each attempt
    context_trimmed = False  # True after first prompt-too-large trim

    print(f"  {DIM}Finding relevant files ({PLANNER_MODEL})...{RESET}")
    rel_files = find_relevant_files(task, project_root)
    print(f"  Files: {', '.join(rel_files) or '(none found)'}")

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
                    passed_c, output_c = validate(baseline_errors)
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
            f"  {DIM}Attempt {phase_attempts + 1}/{PHASE_MAX_ATTEMPTS} — coding"
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
            if phase_attempts >= PHASE_MAX_ATTEMPTS:
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
            if phase_attempts >= PHASE_MAX_ATTEMPTS:
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
        written, pattern_errs = write_changes(changes, project_root, test_only=is_test)
        last_written = written

        if pattern_errs:
            errors = pattern_errs + ("\n\n" + errors if errors else "")
            errors_seen.append("bad_pattern")
            print(f" {YELLOW}blocked by bad patterns — retrying{RESET}")
            continue

        changed_list = ", ".join(written)
        print(f" wrote {len(written)} file(s): {changed_list[:60]}")

        print(f"  {DIM}Validating...{RESET}", end="", flush=True)
        passed, output = validate(baseline_errors)

        with open(log_file, "a") as log:
            log.write(f"\n### Attempt {attempt}\nFiles changed: {changed_list}\n")
            log.write(f"Validation: {'PASSED' if passed else 'FAILED'}\n{output}\n")

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
            fix_count, _ = af.apply_mechanical_fixes(output, project_root)
            if fix_count > 0:
                print(f"  {DIM}⚙  Auto-fixed {fix_count} issue(s) — re-validating...{RESET}", end="", flush=True)
                passed2, output2 = validate(baseline_errors)
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

        # Step 2: qwen advisor
        advisor_hint = ""
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
            errors_seen.append(f"qwen:{','.join(advice.get('categories', []))}")
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
        if advisor_hint:
            errors = f"⚠️  Advisor note: {advisor_hint}\n\n{errors}"

        thrashing = phase_attempts >= PHASE_MAX_ATTEMPTS
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
                            passed_c, output_c = validate(baseline_errors)
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
    parser.add_argument("--worker-id", type=int, default=0,
                        help="Worker index for parallel execution (0-based)")
    parser.add_argument("--stride",    type=int, default=1,
                        help="Total number of parallel workers (1 = sequential)")
    parser.add_argument("--budget-multiplier", type=float, default=None,
                        help="Override BUDGET_MULTIPLIER for this run "
                             "(e.g. pass --stride value so parallel workers "
                             "get proportionally more time to account for "
                             "Ollama queue depth)")
    parser.add_argument("--max-tier", type=int, default=None,
                        help="Cap the tier ladder at N (1-indexed). "
                             "Tasks that fail at tier N are written to "
                             "logs/tier2_queue.jsonl for a --deep run instead "
                             "of escalating. --quick sets this to 1.")
    parser.add_argument("--quick", action="store_true",
                        help="Tier-1-only pass: fast sweep using only the "
                             "smallest model. Failures are queued in "
                             "logs/tier2_queue.jsonl for a follow-up --deep run.")
    parser.add_argument("--deep", action="store_true",
                        help="Process only tasks queued by a previous --quick "
                             "run (reads logs/tier2_queue.jsonl), starting at "
                             "tier 2. Clears the queue file when done.")
    parser.add_argument("--commit-sprint", action="store_true",
                        help="Commit and push sprint planning artifacts "
                             "(ROADMAP.md, task_graph.json, .roorules) before "
                             "starting the work loop. Use after plan_week.py "
                             "so cloud workers can immediately pull the DAG.")
    args = parser.parse_args()

    if args.project:
        project_root = os.path.abspath(args.project)
        if not os.path.isdir(project_root):
            print(f"⚠  Not found: {project_root}")
            sys.exit(1)
        os.chdir(project_root)
    else:
        project_root = os.getcwd()

    # ── Load per-project config (.sovereign_config.json) ─────────────────────
    # Must happen before anything else so LOCKED_FILES is populated before
    # any file reads, writes, or planning steps touch the project.
    _load_project_config(project_root)

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
            print(f"  {YELLOW}pubspec.yaml changed or cache missing — running flutter pub get...{RESET}")
            result = subprocess.run(
                ["flutter", "pub", "get"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                print(f"  {GREEN}✓ flutter pub get succeeded{RESET}")
            else:
                print(f"  {RED}⚠ flutter pub get failed (continuing anyway):\n{result.stderr[:400]}{RESET}")

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
    # --quick  → cap at tier 1; failures go into logs/tier2_queue.jsonl
    # --deep   → only process tasks from tier2_queue.jsonl, starting at tier 2
    # --max-tier N → explicit cap (overrides --quick when both given)
    if args.quick and not args.max_tier:
        args.max_tier = 1
    run_max_tier_idx: int | None = (
        min(args.max_tier, len(TIER_MODELS)) if args.max_tier else None
    )
    run_start_tier_idx: int = 0

    tasks = parse_all_tasks()

    # ── Quick mode: skip tasks already queued for --deep ──────────────────────
    # If tier2_queue.jsonl exists from a prior --quick run, those tasks already
    # failed at tier1 and re-attempting them would just re-queue them.  Skip
    # them so a restarted --quick run only touches genuinely untried tasks.
    if args.quick or run_max_tier_idx is not None:
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
        run_start_tier_idx = 1   # start at tier2 (0-indexed)
        # Deep mode gets a generous budget by default (2× normal multiplier)
        if args.budget_multiplier is None:
            args.budget_multiplier = BUDGET_MULTIPLIER * 2

    # Apply stride: worker K handles tasks at positions K, K+stride, K+2×stride …
    if args.stride > 1:
        tasks = [t for i, t in enumerate(tasks) if i % args.stride == args.worker_id]

    if not tasks:
        print(f"{YELLOW}No unchecked tasks found anywhere in ROADMAP.md.{RESET}")
        print(f"Add tasks to ROADMAP.md or run plan_week.py first.")
        sys.exit(0)

    mode_label = "  ⚡ QUICK (tier-1 only)" if args.quick else (
                 "  🔬 DEEP (tier-2+)" if args.deep else "")
    worker_label = f" · worker {args.worker_id}/{args.stride}" if args.stride > 1 else ""
    tier_range = (f"tier{run_start_tier_idx + 1}–{run_max_tier_idx or len(TIER_MODELS)}"
                  if (run_start_tier_idx or run_max_tier_idx) else
                  f"{len(TIER_MODELS)} tiers")
    print(f"{BOLD}{'━'*56}{RESET}")
    print(f"{BOLD}  🤖  Autonomous work loop — {today_str}{worker_label}{RESET}")
    if mode_label:
        print(f"{BOLD}{mode_label}{RESET}")
    print(f"{BOLD}  {len(tasks)} tasks · {tier_range} · up to {PHASE_MAX_ATTEMPTS} attempts/tier · YOLO{RESET}")
    print(f"{BOLD}{'━'*56}{RESET}\n")

    # ── Verify Model Availability ─────────────────────────────────────────────
    # Fast check at startup to ensure the cascade won't fail halfway through a run
    if not args.dry_run:
        print(f"  {DIM}Verifying model availability...{RESET}")
        # Only check models that will actually be used in this run
        active_tiers = TIER_MODELS[run_start_tier_idx:(run_max_tier_idx or len(TIER_MODELS))]
        for m in list(dict.fromkeys(active_tiers + [PLANNER_MODEL, ADVISOR_MODEL])):
            ensure_model_exists(m)
        print(f"  {DIM}All models ready.{RESET}\n")

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

    # Dependency tracking: when an implement task fails/skips, any immediately
    # following test task is auto-blocked and queued for the retry pass instead
    # of running against a broken or missing implementation.
    last_incomplete: int | None = None   # task index of last failed/skipped task

    for i, task_line in enumerate(tasks, 1):
        if i < args.start_at:
            print(f"{DIM}[{i}/{len(tasks)}] Skipping: {task_text(task_line)[:60]}{RESET}")
            continue

        task = task_text(task_line)
        is_test = _is_test_task(task)

        # ── Feature/Test filtering ───────────────────────────────────────────
        if args.features_only and is_test:
            print(f"  {DIM}[deferred] Task {i} is a test task (features-only mode){RESET}")
            continue
        if args.tests_only and not is_test:
            print(f"  {DIM}[skipping] Task {i} is a feature task (tests-only mode){RESET}")
            continue

        print(f"\n{BOLD}[{i}/{len(tasks)}] {task[:70]}{'...' if len(task) > 70 else ''}{RESET}")

        with open(log_file, "a") as f:
            f.write(f"\n## Task {i}: {task}\n")

        if args.dry_run:
            print(f"  {DIM}(dry-run){RESET}")
            continue

        # Dependency block: if the immediately preceding task did not complete
        # and this is a test task, skip it now and retry later (after the
        # implement task has had a chance to succeed in the retry pass).
        if _is_test_task(task) and last_incomplete is not None and i == last_incomplete + 1:
            print(
                f"  {YELLOW}⏭  Dependency-blocked: task {last_incomplete} did not complete — "
                f"test task deferred to retry pass{RESET}"
            )
            with open(log_file, "a") as f:
                f.write(f"Dependency-blocked: preceding task {last_incomplete} failed/skipped.\n")
            skipped_count += 1
            skipped_tasks.append((i, task))
            retry_queue.append((i, task_line, task))
            last_incomplete = i   # chain: mark this task as incomplete too
            continue

        branch = _branch_start(project_root, i, task)
        if branch:
            print(f"  {DIM}⎇  Branch: {branch}{RESET}")

        result = run_task(task, project_root, log_file, task_idx=i,
                         budget_s=get_first_pass_budget(),
                         max_tier_idx=run_max_tier_idx,
                         start_tier_idx=run_start_tier_idx)

        if result is True:
            if branch:
                _branch_merge(project_root, branch, i, task)
                print(f"  {DIM}⎇  Merged {branch} → main{RESET}")
            mark_done(task_line)
            print(f"  {GREEN}✓ Marked done in ROADMAP.md{RESET}")
            passed_count += 1
            last_incomplete = None   # successful task resets the chain
        elif result == "skipped":
            if branch:
                _branch_abandon(project_root, branch)
            # In a capped run (--quick / --max-tier), write failures to the
            # tier2 queue so a follow-up --deep run can pick them up.
            if run_max_tier_idx is not None and run_max_tier_idx < len(TIER_MODELS):
                _write_tier2_queue(project_root, i, task, [])
                print(f"  {YELLOW}↳ queued for --deep run{RESET}")
            skipped_count += 1
            skipped_tasks.append((i, task))
            retry_queue.append((i, task_line, task))
            last_incomplete = i
            print(f"  {YELLOW}⏭  Skipped — queued for retry pass{RESET}")
        else:
            if branch:
                _branch_abandon(project_root, branch)
            failed_count += 1
            retry_queue.append((i, task_line, task))
            last_incomplete = i
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

            result = run_task(task, project_root, log_file,
                              task_idx=task_idx, budget_s=retry_budget)
            if result is True:
                mark_done(task_line)
                print(f"  {GREEN}✓ Marked done in ROADMAP.md{RESET}")
                retry_passed += 1
            elif result == "skipped":
                retry_skipped += 1
                print(f"  {YELLOW}⏭  Still skipped after retry{RESET}")
            else:
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
