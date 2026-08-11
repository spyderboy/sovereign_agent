"""
hints.py — pattern gates for the sovereign worker, assembled from packs.

BAD_PATTERNS  : checked against every proposed file before it is written to
                disk. If matched, the file is rejected and the hint is fed back
                to the model.
ERROR_HINTS   : checked against analyzer/compiler output after validation
                fails. Matching hints are prepended to the error message on the
                next attempt.

WHY THIS IS A LOADER AND NOT A LIST
-----------------------------------
Until 2026-07-25 this file was a single 917-line list applied to every project.
109 entries, and the split showed 49 of them (45%) were Galaxican/astro-flux
specifics — project class names, concrete lib/ paths, and policy statements
like "Firebase packages are NOT in this project."

That last one is the reason this had to change. Pointed at a Firebase-backed
codebase, the old hints.py would have instructed the model to delete correct
imports on sight. There was no way to turn it off: .sovereign_config.json could
only ADD patterns, never remove them.

Packs live in hint_packs/ and are selected per project:

    dart_core       Dart language + analyzer traps. Any Dart project.
    flutter_ui      Flutter widget/UI layer. Any Flutter app.
    dart_riverpod   Riverpod state management. Only if the project uses it.
    dart_flame      Flame game engine. Only for Flame games.
    galaxican       Galaxican/astro-flux domain. Only for those projects.

Selection, in .sovereign_config.json:

    "hint_packs": ["dart_core", "flutter_ui"]     explicit; overrides defaults
    "disable_bad_patterns": ["withOpacity"]       drop by pattern substring
    "additional_bad_patterns": [{"pattern":..., "hint":...}]
    "additional_error_hints":  [{"pattern":..., "hint":...}]

Omit "hint_packs" and the language's defaults load (see DEFAULT_PACKS). A new
Dart project starts with dart_core + flutter_ui and zero Galaxican patterns.

CONTRACT
--------
BAD_PATTERNS and ERROR_HINTS are module-level lists mutated IN PLACE by
load_packs(). work.py does `from hints import BAD_PATTERNS, ERROR_HINTS` at
import time and _load_project_config extends them afterwards, so rebinding
these names would silently detach the worker from its own config. Mutate,
never reassign.

Adding a new trap: put it in the narrowest pack that could ever need it. When
in doubt, narrower — a missing hint costs one attempt, a wrong hint costs every
attempt on every project that inherits it.

The pre-split file is preserved as hints_legacy.py for reference; nothing
imports it except tools/split_hints.py.
"""

from __future__ import annotations

import importlib

# Mutated in place by load_packs(). Never rebind — see CONTRACT above.
BAD_PATTERNS: list[tuple[str, str]] = []
ERROR_HINTS: list[tuple[str, str]] = []

AVAILABLE_PACKS = ("dart_core", "flutter_ui", "dart_riverpod", "dart_flame",
                   "galaxican")

# What loads when .sovereign_config.json says nothing. Conservative by design:
# generic language traps only. Engine- and project-specific packs are opt-in.
DEFAULT_PACKS: dict[str, tuple[str, ...]] = {
    "dart":       ("dart_core", "flutter_ui"),
    "flutter":    ("dart_core", "flutter_ui"),
    "go":         (),
    "typescript": (),
    "python":     (),
    "swift":      (),
    "generic":    (),
}

_loaded: list[str] = []


def loaded_packs() -> list[str]:
    return list(_loaded)


def load_packs(language: str | None = None,
               packs: list[str] | None = None,
               disable: list[str] | None = None,
               verbose: bool = True) -> list[str]:
    """Populate BAD_PATTERNS / ERROR_HINTS from the selected packs.

    language — used to pick DEFAULT_PACKS when `packs` is None
    packs    — explicit pack names; unknown names are reported and skipped
    disable  — substrings; any entry whose PATTERN contains one is dropped

    Returns the pack names actually loaded. Idempotent: calling twice replaces
    the contents rather than doubling them.
    """
    if packs is None:
        key = (language or "generic").strip().lower()
        names = list(DEFAULT_PACKS.get(key, ()))
    else:
        names = list(packs)

    bad: list[tuple[str, str]] = []
    errs: list[tuple[str, str]] = []
    ok: list[str] = []

    for name in names:
        if name not in AVAILABLE_PACKS:
            if verbose:
                print(f"  ⚠  unknown hint pack '{name}' — available: "
                      f"{', '.join(AVAILABLE_PACKS)}")
            continue
        try:
            mod = importlib.import_module(f"hint_packs.{name}")
        except Exception as exc:
            if verbose:
                print(f"  ⚠  could not load hint pack '{name}': {exc}")
            continue
        bad.extend(getattr(mod, "BAD_PATTERNS", []))
        errs.extend(getattr(mod, "ERROR_HINTS", []))
        ok.append(name)

    if disable:
        before = len(bad) + len(errs)
        bad = [e for e in bad if not any(d in e[0] for d in disable)]
        errs = [e for e in errs if not any(d in e[0] for d in disable)]
        dropped = before - len(bad) - len(errs)
        if dropped and verbose:
            print(f"  ✓  disabled {dropped} pattern(s) via disable_bad_patterns")

    BAD_PATTERNS[:] = bad
    ERROR_HINTS[:] = errs
    _loaded[:] = ok

    if verbose:
        print(f"  ✓  hint packs: {', '.join(ok) if ok else '(none)'} "
              f"— {len(BAD_PATTERNS)} bad-pattern(s), {len(ERROR_HINTS)} hint(s)")
        if packs is None and ok:
            unloaded = [p for p in AVAILABLE_PACKS if p not in ok]
            if unloaded:
                # Existing projects predate hint_packs and will silently load
                # fewer patterns than before the split. Say so rather than let
                # a gate quietly stop firing.
                print(f"     (defaults for '{language}'. Not loaded: "
                      f"{', '.join(unloaded)}. Set \"hint_packs\" in "
                      f".sovereign_config.json to choose explicitly.)")
    return ok
