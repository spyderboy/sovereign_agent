"""Per-project config (.sovereign_config.json) loading into sov.state.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from hints import BAD_PATTERNS
from hints import ERROR_HINTS
import hints
import json
import os
import re as _re
from sov.config import (
    describe as _describe_config,
)
from sov import state
from sov.files import LOCKED_FILES
from sov.ui import DIM, RESET, YELLOW


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
            # MULTILINE or `^` only ever matches the first line of a file.
            # Two of this project's guards are `^`-anchored — the locked-type
            # and locked-constant redeclare checks — and both sat inert for
            # days while three files merged green with their own copies of
            # locked types (2026-08-12).
            compiled = _re.compile(entry["pattern"], _re.MULTILINE)
            # Optional except_paths (2026-08-22): see check_bad_patterns'
            # docstring — a guard can legitimately need to exempt the one
            # file whose job is to do the thing it otherwise forbids.
            except_paths = entry.get("except_paths") or entry.get("except_path")
            if isinstance(except_paths, str):
                except_paths = [except_paths]
            if except_paths:
                BAD_PATTERNS.append((compiled, entry["hint"], tuple(except_paths)))
            else:
                BAD_PATTERNS.append((compiled, entry["hint"]))

    # ── Extra error hints ─────────────────────────────────────────────────────
    extra_hints = cfg.get("additional_error_hints", [])
    for entry in extra_hints:
        if isinstance(entry, dict) and "pattern" in entry and "hint" in entry:
            ERROR_HINTS.append((_re.compile(entry["pattern"]), entry["hint"]))

    # ── Always-include context files ──────────────────────────────────────────
    ai = cfg.get("context_always_include", [])
    if isinstance(ai, list) and all(isinstance(p, str) for p in ai):
        state.PROJECT_ALWAYS_INCLUDE = list(ai)
        if ai:
            print(f"  {DIM}Loaded {len(ai)} always-include context file(s){RESET}")
            # The ladder is now data (profiles/*.toml + env). Print which one
            # is actually in force — otherwise the only way to know is to
            # wait for a model name to scroll past on the first attempt.
            print(f"  {DIM}{_describe_config()}{RESET}")

    # ── RAG subsystem (opt-in) ─────────────────────────────────────────────────
    state.RAG_ENABLED = bool(cfg.get("rag_enabled", False))
    if state.RAG_ENABLED:
        print(f"  {DIM}RAG subsystem enabled — hybrid retrieval + failure-memory active{RESET}")

    # ── Per-project validation commands + language ────────────────────────────
    vc = cfg.get("validate_commands", [])
    if isinstance(vc, list) and vc and all(isinstance(c, str) for c in vc):
        state.PROJECT_VALIDATE_COMMANDS = list(vc)
        print(f"  {DIM}Loaded {len(vc)} project validate command(s) — "
              f"built-in flutter/pytest/npm gates disabled{RESET}")
    lang = cfg.get("language")
    if isinstance(lang, str) and lang:
        state.PROJECT_LANGUAGE = lang.lower()

    proj_id = cfg.get("project")
    state.PROJECT_ID = proj_id if isinstance(proj_id, str) and proj_id else os.path.basename(
        os.path.abspath(project_root))

    total = len(exact) + len(prefixes)
    if total:
        print(f"  {DIM}Loaded project config: {len(exact)} locked paths, "
              f"{len(prefixes)} locked prefixes{RESET}")

    return cfg
