"""Project file access: source discovery, relevant-file selection, reading
with truncation, backups, the locked-files registry, and writing model output
(mechanical rewrites, search/replace patches, API-guide upkeep).

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from pathlib import Path
import grounders
import json
import os
import re
from sov.config import (
    PLANNER_MODEL,
)
from sov import state
from sov.guards import check_bad_patterns
from sov.llm import ollama
from sov.ui import DIM, RED, RESET, YELLOW


# 2026-09-09: above this many lines, ask for SEARCH/REPLACE blocks instead of
# a full-file rewrite for the task's own target file (see implement_task's
# diff_mode_block and write_changes' _apply_search_replace). A full rewrite
# of a 450+-line file timed out twice at 30 min each on this machine for a
# ~20-line insertion — the model has to regenerate everything, including the
# ~430 lines that were already correct, every single attempt.
_DIFF_MODE_LINE_THRESHOLD = 150


# NOTE: task_graph.json is NOT regenerated at run time. work.py drives off
# ROADMAP.md via parse_all_tasks(); task_graph.json is the durable DAG record
# maintained by make_graph.py / plan_week.py only. A prior auto-sync helper
# lived here and was removed 2026-07-17 — regenerating the graph mid-run
# clobbered completed-phase task state and desynced it from ROADMAP.


# ─── File discovery ───────────────────────────────────────────────────────────

def all_source_files(project_root: str) -> list[str]:
    """Return all dart/py/js/go/sh source files relative to project root.

    2026-07-13: added ".go" — its absence meant find_relevant_files returned
    [] for Go projects, so every worker coded with ZERO context and
    hallucinated field names, math helpers, and imports.

    2026-09-08: added ".sh" — same failure mode for the "bash" language
    project (sovereign_agent's own self-improvement pass against
    supervisor.sh): the target file could never appear in the planner's
    candidate list, so it picked unrelated files from elsewhere in the tree
    instead.
    """
    exts = {".dart", ".py", ".js", ".ts", ".go", ".sh"}
    skip = {"build", ".dart_tool", ".git", "node_modules", ".fvm", ".venv", "venv",
            "logs", "site-packages", "dist-packages", ".tox", "vendor",
            "Pods", ".gradle", "reference", ".pub-cache"}
    # A virtualenv can be named anything (2026-08-11: `mlx_env` inside
    # witches_bricks put 4,722 transformers files into the planner prompt —
    # 92k tokens, 757% of context, so the planner silently fell back to
    # keywords on EVERY task). Name-based skipping cannot catch that; the
    # marker file can.
    roots = {p.parent.name for p in Path(project_root).glob("*/pyvenv.cfg")}
    skip |= roots
    result = []
    for p in Path(project_root).rglob("*"):
        # Match skip names against the path RELATIVE to the project: a
        # project that itself lives under e.g. .git/ or build/ must not have
        # every file skipped (2026-09-28, parallel-worker worktrees).
        rel = p.relative_to(project_root)
        if p.suffix in exts and not any(s in rel.parts for s in skip):
            result.append(str(rel))
    if len(result) > 1200:
        print(f"  {YELLOW}⚠ {len(result)} source files — the planner prompt will "
              f"be large. Check for a vendored dependency tree in the project."
              f"{RESET}")
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


_REWRITES: list[tuple[str, str, str]] = [
    # (pattern, replacement, why) — each must be UNCONDITIONALLY correct.
    (r"\.withOpacity\(\s*([^()]*?)\s*\)", r".withValues(alpha: \1)",
     "withOpacity is deprecated in this Flutter; withValues(alpha:) is the "
     "exact replacement"),
]


def _mechanical_rewrites(rel_path: str, content: str) -> str:
    """Fix what a guard would only have rejected.

    A bad-pattern guard can say no, and saying no costs a whole generation
    cycle: the model regenerates the entire file, usually reintroducing the
    same construct because the construct is what its training data is full of.
    `.withOpacity()` was blocked five times on one file on 2026-08-13 and
    consumed arena.dart's entire task budget without `flutter analyze` ever
    running once — while the correct spelling sat three times over in
    socket.dart, which was in the model's context the whole time.

    When the repair is a deterministic text transformation, doing it is
    strictly better than refusing. The bar for entry here is high and narrow:
    a rewrite must be correct in EVERY context, with no judgement, or it does
    not belong in this list — a wrong rewrite is far worse than a block,
    because it lands silently and analyzes clean.
    """
    if not rel_path.endswith(".dart"):
        return content
    for pattern, repl, _why in _REWRITES:
        content = re.sub(pattern, repl, content)
    return content


_SEARCH_REPLACE_RE = re.compile(
    r"<{7} SEARCH\n(.*?)\n={7}\n(.*?)\n>{7} REPLACE", re.DOTALL,
)


def _is_search_replace_format(content: str) -> bool:
    return "<<<<<<< SEARCH" in content and ">>>>>>> REPLACE" in content


def _apply_search_replace(rel_path: str, patch_text: str,
                          old_content: str) -> tuple[str | None, str | None]:
    """Apply one or more SEARCH/REPLACE blocks to old_content.

    Returns (new_content, None) on success, or (None, error_message) — fed
    back to the model on the next attempt the same way a validation error is
    — when a SEARCH block's text isn't found verbatim (the model paraphrased
    or mis-copied it instead of quoting the file it was shown).
    """
    blocks = _SEARCH_REPLACE_RE.findall(patch_text)
    if not blocks:
        return None, (
            f"MALFORMED SEARCH/REPLACE [{rel_path}]: no valid "
            f"<<<<<<< SEARCH / ======= / >>>>>>> REPLACE block found. Each block "
            f"needs all three markers, each on its own line."
        )
    new_content = old_content
    for search, replace in blocks:
        if search not in new_content:
            return None, (
                f"SEARCH BLOCK NOT FOUND [{rel_path}]: the text between "
                f"<<<<<<< SEARCH and ======= does not appear verbatim in the "
                f"current file. Copy the exact lines from 'Current file contents' "
                f"above — including whitespace and indentation — do not retype "
                f"them from memory."
            )
        new_content = new_content.replace(search, replace, 1)
    return new_content, None


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
    changes = {p: _mechanical_rewrites(p, c) for p, c in changes.items()}
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
        if state.PROJECT_LANGUAGE == "go" and rel_path.endswith("_test.go"):
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
        # SEARCH/REPLACE (2026-09-09, see implement_task's diff_mode_block):
        # convert into the full new content HERE, before any downstream check
        # (bad-pattern, grounding, destructive-edit) — every one of those
        # operates on real file content and would misfire against raw
        # <<<<<<< SEARCH markers otherwise.
        if isinstance(content, str) and _is_search_replace_format(content):
            _full_for_patch = os.path.join(project_root, rel_path)
            if not os.path.exists(_full_for_patch):
                msg = (
                    f"INVALID FORMAT [{rel_path}]: SEARCH/REPLACE blocks were used but "
                    f"this file does not exist yet. New files must be returned as "
                    f"complete file content, not a patch against nothing."
                )
                print(f"  {RED}(blocked SEARCH/REPLACE on new file: {rel_path}){RESET}")
                pattern_errors.append(msg)
                continue
            content, _sr_err = _apply_search_replace(
                rel_path, content, open(_full_for_patch).read())
            if _sr_err:
                print(f"  {RED}(SEARCH/REPLACE failed to apply: {rel_path}){RESET}")
                pattern_errors.append(_sr_err)
                continue
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
        _grounder = grounders.for_language(state.PROJECT_LANGUAGE)
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
        # Destructive-edit guard: reject a rewrite that discards most of an
        # existing file's content. Observed directly (2026-09-01/02,
        # old-car-radio): small local models attempting a 1-2 line addition
        # to a large existing file (station_provider.dart, 640 lines)
        # instead regenerated a truncated ~40-line stub, deleting ~93% of
        # the file — twice, on two unrelated tasks. flutter analyze then
        # failed with errors in files this task never touched (every caller
        # of the deleted provider), which the "no error names a file this
        # task wrote" heuristic misread as unrelated to this task and let
        # the tier ladder keep retrying instead of ever blocking the write.
        # Compare against the file's CURRENT on-disk content (not the task's
        # pre-attempt backup) so this also catches a second destructive
        # rewrite stacked on an already-corrupted first attempt within the
        # same task.
        if os.path.exists(full):
            old_content = open(full).read()
            old_lines = old_content.count("\n") + 1
            new_lines = content.count("\n") + 1
            if old_lines >= 15 and new_lines <= old_lines * 0.5 and (old_lines - new_lines) >= 30:
                pct = 100 * (old_lines - new_lines) / old_lines
                msg = (
                    f"DESTRUCTIVE EDIT REJECTED [{rel_path}]: this rewrite would shrink "
                    f"the file from {old_lines} to {new_lines} lines "
                    f"({old_lines - new_lines} lines deleted, {pct:.0f}%). The task asked "
                    f"for a small, additive change — you regenerated the whole file and "
                    f"lost most of its content instead. Make the SMALLEST possible change "
                    f"to the EXISTING file content; do not rewrite it from scratch. If this "
                    f"task genuinely requires deleting this much code, it needs a human to "
                    f"confirm — do not do it automatically."
                )
                print(
                    f"  {RED}(blocked destructive edit: {rel_path} "
                    f"{old_lines}→{new_lines} lines, -{pct:.0f}%){RESET}"
                )
                pattern_errors.append(msg)
                continue  # don't write this file
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


def backup_files(paths: list[str], project_root: str) -> dict[str, str]:
    return {p: open(os.path.join(project_root, p)).read()
            for p in paths if os.path.exists(os.path.join(project_root, p))}


def restore_files(backups: dict[str, str], project_root: str):
    for rel, content in backups.items():
        with open(os.path.join(project_root, rel), "w") as f:
            f.write(content)
