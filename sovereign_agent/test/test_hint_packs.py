"""
Tests for the hint-pack split (hints.py + hint_packs/).

The guarantee that matters: the split lost nothing, and a project that is not
Galaxican no longer inherits Galaxican's traps.

Run:  python test/test_hint_packs.py
"""

import importlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hints             # noqa: E402
import hints_legacy      # noqa: E402  the pre-split file, kept for comparison


def all_pack_entries():
    bad, errs = [], []
    for name in hints.AVAILABLE_PACKS:
        mod = importlib.import_module(f"hint_packs.{name}")
        bad.extend(mod.BAD_PATTERNS)
        errs.extend(mod.ERROR_HINTS)
    return bad, errs


# ── Migration fidelity ───────────────────────────────────────────────────────

def test_union_of_packs_equals_original_exactly():
    bad, errs = all_pack_entries()
    assert sorted(bad) == sorted(hints_legacy.BAD_PATTERNS)
    assert sorted(errs) == sorted(hints_legacy.ERROR_HINTS)


def test_no_entry_lands_in_two_packs():
    seen = {}
    for name in hints.AVAILABLE_PACKS:
        mod = importlib.import_module(f"hint_packs.{name}")
        for lst in ("BAD_PATTERNS", "ERROR_HINTS"):
            for entry in getattr(mod, lst):
                assert entry[0] not in seen, (
                    f"{entry[0][:50]} in both {seen.get(entry[0])} and {name}")
                seen[entry[0]] = name


def test_every_pack_is_importable_and_well_formed():
    for name in hints.AVAILABLE_PACKS:
        mod = importlib.import_module(f"hint_packs.{name}")
        for lst in ("BAD_PATTERNS", "ERROR_HINTS"):
            for entry in getattr(mod, lst):
                assert isinstance(entry, tuple) and len(entry) == 2, entry
                assert isinstance(entry[0], str) and isinstance(entry[1], str)
                re.compile(entry[0])   # every pattern must still compile


# ── Isolation: the point of the exercise ─────────────────────────────────────

PROJECT_NOUNS = re.compile(
    r"lib/[a-z_]+/|\bStar\b|Squad|Nova|Galax|astro|Astro|Fusion|Mote|"
    r"GameCore|gameService|difficultyProvider|CombatAttack|Level001|"
    r"LocalPersistenceService", re.IGNORECASE)


def test_portable_packs_carry_no_project_nouns():
    """dart_core / flutter_ui / dart_flame must be safe on any project."""
    for name in ("dart_core", "flutter_ui", "dart_flame"):
        mod = importlib.import_module(f"hint_packs.{name}")
        for lst in ("BAD_PATTERNS", "ERROR_HINTS"):
            for pattern, hint in getattr(mod, lst):
                assert not PROJECT_NOUNS.search(pattern + hint), \
                    f"{name} leaks project specifics: {pattern[:60]}"


def test_fresh_dart_project_gets_no_galaxican_patterns():
    hints.load_packs(language="dart", verbose=False)
    assert hints.loaded_packs() == ["dart_core", "flutter_ui"]
    galaxican = importlib.import_module("hint_packs.galaxican")
    loaded = {p for p, _ in hints.BAD_PATTERNS} | {p for p, _ in hints.ERROR_HINTS}
    for pattern, _ in galaxican.BAD_PATTERNS + galaxican.ERROR_HINTS:
        assert pattern not in loaded, pattern[:60]


def test_firebase_hint_is_not_inherited():
    """The concrete hazard: the old hints.py told every project that Firebase
    packages do not exist. On a Firebase-backed codebase that instructs the
    model to delete correct imports."""
    hints.load_packs(language="dart", verbose=False)
    blob = " ".join(p + h for p, h in hints.BAD_PATTERNS + hints.ERROR_HINTS)
    assert "firebase" not in blob.lower()
    # …and it is still available to the project it belongs to.
    hints.load_packs(packs=["galaxican"], verbose=False)
    blob = " ".join(p + h for p, h in hints.ERROR_HINTS)
    assert "firebase" in blob.lower()


def test_non_dart_languages_load_nothing():
    for lang in ("go", "typescript", "python", "swift", None):
        hints.load_packs(language=lang, verbose=False)
        assert hints.BAD_PATTERNS == [] and hints.ERROR_HINTS == [], lang


# ── Loader behaviour ─────────────────────────────────────────────────────────

def test_explicit_packs_override_defaults():
    hints.load_packs(language="dart", packs=["dart_flame"], verbose=False)
    assert hints.loaded_packs() == ["dart_flame"]


def test_disable_drops_matching_patterns():
    hints.load_packs(packs=["flutter_ui"], verbose=False)
    before = len(hints.BAD_PATTERNS) + len(hints.ERROR_HINTS)
    assert any("withOpacity" in p for p, _ in hints.BAD_PATTERNS)
    hints.load_packs(packs=["flutter_ui"], disable=["withOpacity"], verbose=False)
    after = len(hints.BAD_PATTERNS) + len(hints.ERROR_HINTS)
    assert after < before
    assert not any("withOpacity" in p for p, _ in hints.BAD_PATTERNS)


def test_load_is_idempotent():
    hints.load_packs(language="dart", verbose=False)
    first = (len(hints.BAD_PATTERNS), len(hints.ERROR_HINTS))
    hints.load_packs(language="dart", verbose=False)
    assert (len(hints.BAD_PATTERNS), len(hints.ERROR_HINTS)) == first


def test_lists_are_mutated_in_place_not_rebound():
    """work.py does `from hints import BAD_PATTERNS` at import time. If
    load_packs rebound the name, the worker would hold a detached empty list
    and every gate would silently stop firing."""
    from hints import BAD_PATTERNS as captured
    hints.load_packs(packs=["dart_flame"], verbose=False)
    assert captured is hints.BAD_PATTERNS
    assert len(captured) > 0


def test_unknown_pack_is_skipped_not_fatal():
    ok = hints.load_packs(packs=["dart_core", "nonexistent_pack"], verbose=False)
    assert ok == ["dart_core"]


def test_all_packs_together_still_equal_the_original():
    hints.load_packs(packs=list(hints.AVAILABLE_PACKS), verbose=False)
    assert len(hints.BAD_PATTERNS) == len(hints_legacy.BAD_PATTERNS)
    assert len(hints.ERROR_HINTS) == len(hints_legacy.ERROR_HINTS)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}\n      {str(e)[:300]}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name} — ERROR {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
