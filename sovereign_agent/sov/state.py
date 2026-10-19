"""Runtime state rebound while a run is live, shared by every sov module.

These were module globals in work.py reassigned via `global` (project config
loading, parallel-worker setup, a CLI override). A `from x import NAME` copy
would go stale on reassignment, so every reader uses `state.NAME`.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from sov.config import CLAUDE_ENABLED as _CLAUDE_ENABLED_DEFAULT
from sov.config import ESCALATION_MODEL as _ESCALATION_MODEL

# The CLI may force Claude escalation off for this run (see work.main).
CLAUDE_ENABLED = _CLAUDE_ENABLED_DEFAULT
# The final tier after local models: Claude, or the self-hosted ESCALATION_MODEL.
FINAL_TIER_ENABLED = bool(_CLAUDE_ENABLED_DEFAULT or _ESCALATION_MODEL)

# Per-attempt thinking override set by the runner (OLLAMA_THINK_RETRIES);
# None means use OLLAMA_THINK.
THINK_OVERRIDE: bool | None = None
# Files the current attempt may only change with SEARCH/REPLACE blocks: an existing file a task edits
# (set per attempt by implement_task, enforced by write_changes). See prompting._file_on_main.
REQUIRE_PATCH: set[str] = set()


# ─── Parallel-worker isolation (2026-09-28) ───────────────────────────────────
# Parallel workers used to share ONE working tree and git index with no
# coordination: every worker's between-task `checkout -f` + `clean -fd` wiped
# whatever another worker had written mid-attempt, and merges collided
# ("Merging is not possible because you have unmerged files" — xmeasure's
# first 2-worker sweep died on task 4 this way). Now, when stride > 1, each
# worker runs in its own git worktree (under ~/.cache/xanadu-worktrees/, so
# the primary checkout never sees it), on a detached HEAD at main's tip. Only
# the primary checkout ever has main checked out, and every write to main —
# task merges and ROADMAP [x] marks — happens there under an flock.
# INTEGRATION_ROOT is None in single-worker mode, and everything below
# degrades to the original behaviour.
INTEGRATION_ROOT: str | None = None


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

# RAG subsystem (rag_*.py), opt-in via .sovereign_config.json "rag_enabled".
# Default off: existing projects are completely unaffected until they opt in.
RAG_ENABLED: bool = False
ESCALATE_LOG   = "logs/escalate.md"

# The full text of the task run_task is working on. velocity.jsonl stores the
# text truncated; telemetry adds a hash of the full text so routing can tell
# an edited task from the old one (see routing.small_failures).
CURRENT_TASK = ""
# Set by run_task when it deferred the task for a missing dependency (no attempt made).
LAST_TASK_DEFERRED = False
