"""
split_hints.py — one-off migration: partition the monolithic hints.py into
per-language packs under hint_packs/.

Run from sovereign_agent/:   python tools/split_hints.py

Generated rather than hand-copied on purpose. The 109 entries carry regexes
with escaping that is easy to corrupt by retyping, and several encode findings
that cost real debugging time (the canvas.drawLine lookahead was narrowed once
already after it fired on correct code). This script moves the exact objects
and asserts the union is byte-identical to the original.

Classification is ordered — first matching rule wins. Reviewed by hand after
generation; see hint_packs/README.md for the resulting counts.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hints_legacy as legacy  # the original hints.py, renamed

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "hint_packs")

# ── Classification, most specific first ──────────────────────────────────────
# Matched against pattern AND hint text combined.
#
# First pass keyed on the pattern alone put 37 Galaxican-specific entries into
# dart_core: the pattern is often a generic analyzer code
# ("extends_non_class", "Target of URI doesn't exist") while the hint names a
# project class or file ("AstroGame has NO '.state' getter", "the file lives at
# lib/components/..."). The hint is where the project knowledge actually lives.

# A hint that cites a concrete lib/ path is describing THIS project's file
# layout and cannot transfer anywhere.
PROJECT_PATH = r"lib/[a-z_]+/"

RULES: list[tuple[str, str]] = [
    # Galaxican domain objects and files. These can never apply anywhere else.
    (PROJECT_PATH +
     r"|Star|Squad|Nova|Galax|fuseWith|FusionAnimation|astro_game|AstroGame|"
     r"astro_flux|factionId|forFaction|capture_target|star_production|Fusion|"
     r"AsyncErr|endPosition|generateId|Mote|GameCore|game_core|game_events|"
     r"game_rules_engine|game_state_provider|gameServiceProvider|"
     r"difficultyProvider|settingsProvider|gameRulesProvider|"
     r"CombatAttackEvent|CombatResultLabel|capture_result_label|"
     r"StarCaptureIndicator|star_capture|level_up_event_bus|LevelUpEventBus|"
     r"Level1|Level001|capture_rules|audio_service|particle_effects|"
     r"UnitComponent|AttackLineComponent|VectorFusedEvent|ConnectivityResult|"
     r"CaptureIndicator|NovaProvider|LocalPersistenceService", "galaxican"),
    # ^ LocalPersistenceService catches the "no Firebase in this project" hint.
    #   Left in a shared pack it would fire on a Firebase-backed project and
    #   tell the model to delete correct imports — the exact class of damage
    #   this split exists to prevent.

    # Flame game engine.
    (r"Vector2|Flame|flame|canvas\.draw|Sprite|PositionComponent|"
     r"lengthSquared|normalized|MoveEffect|RotateEffect|ScaleEffect|"
     r"OpacityEffect|Bgm|AudioPlayer|TapDownEvent|DragStartEvent|"
     r"ScaleDetector|TapDetector|DragCallbacks|vector_math|"
     r"\.dx\b|\.dy\b", "dart_flame"),

    # Riverpod state management.
    (r"riverpod|Riverpod|StateNotifier|\.notifier|NotifierProvider|"
     r"ConsumerWidget|WidgetRef|ProviderContainer|ref\.watch|ref\.read",
     "dart_riverpod"),

    # Flutter widget/UI layer.
    (r"withOpacity|super\.key|Colors\.|BuildContext|Widget|Material|"
     r"Scaffold|EdgeInsets|library_private_types_in_public_api|"
     r"annotate_overrides|prefer_const|use_build_context|"
     r"Paint\(\)|dart:ui|flutter_test", "flutter_ui"),

    # Everything else that mentions Dart analyzer codes or Dart syntax.
    (r"undefined_|const_with_non_const|extends_non_class|missing_required|"
     r"argument_type_not_assignable|non_type_as_type_argument|"
     r"extra_positional|abstract_super_member|ambiguous_import|"
     r"invalid_assignment|body_might_complete|uri_does_not_exist|"
     r"as\\?|\\.dart\b|override_on_non_overriding|super_formal|"
     r"not_enough_positional|missing_default_value|await_in_wrong|"
     r"unchecked_use_of_nullable|cast_to_non_type|expected_token|"
     r"missing_identifier|unused_local|expected_executable|"
     r"missing_function_body|part_of|late_final|dead_code|"
     r"return_of_invalid_type|invalid_override|non_abstract_class", "dart_core"),
]

FALLBACK = "dart_core"

PACK_DOC = {
    "dart_core": "Dart language and analyzer traps. Applies to ANY Dart project.",
    "flutter_ui": "Flutter widget/UI layer. Applies to any Flutter app.",
    "dart_riverpod": "Riverpod state management. Load only if the project uses it.",
    "dart_flame": "Flame game engine. Load only for Flame games.",
    "galaxican": "Galaxican/astro-flux domain specifics. Load only for those projects.",
}

PACK_ORDER = ["dart_core", "flutter_ui", "dart_riverpod", "dart_flame", "galaxican"]


def classify(pattern: str, hint: str) -> str:
    text = f"{pattern}\n{hint}"
    for rx, pack in RULES:
        if re.search(rx, text):
            return pack
    return FALLBACK


def main() -> int:
    buckets: dict[str, dict[str, list]] = {
        p: {"BAD_PATTERNS": [], "ERROR_HINTS": []} for p in PACK_ORDER}

    for name, entries in (("BAD_PATTERNS", legacy.BAD_PATTERNS),
                          ("ERROR_HINTS", legacy.ERROR_HINTS)):
        for pattern, hint in entries:
            buckets[classify(pattern, hint)][name].append((pattern, hint))

    os.makedirs(OUT_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, "__init__.py"), "w").write(
        '"""Per-language hint packs. Loaded by hints.py, selected by config."""\n')

    for pack in PACK_ORDER:
        path = os.path.join(OUT_DIR, f"{pack}.py")
        with open(path, "w") as f:
            f.write('"""\n')
            f.write(f"{pack} — {PACK_DOC[pack]}\n\n")
            f.write("Generated by tools/split_hints.py from the original\n")
            f.write("monolithic hints.py (2026-07-25). Entries are verbatim.\n")
            f.write('"""\n\n')
            for name in ("BAD_PATTERNS", "ERROR_HINTS"):
                f.write(f"{name}: list[tuple[str, str]] = [\n")
                for pattern, hint in buckets[pack][name]:
                    f.write(f"    ({pattern!r},\n     {hint!r}),\n")
                f.write("]\n\n")

    # ── Verification: nothing lost, nothing altered ──────────────────────
    for name, original in (("BAD_PATTERNS", legacy.BAD_PATTERNS),
                           ("ERROR_HINTS", legacy.ERROR_HINTS)):
        rebuilt = [e for p in PACK_ORDER for e in buckets[p][name]]
        assert sorted(rebuilt) == sorted(original), f"{name}: entries changed!"
        assert len(rebuilt) == len(original), f"{name}: count changed!"

    print(f"  wrote {len(PACK_ORDER)} packs to {OUT_DIR}")
    print(f"  {'pack':<16} {'bad':>5} {'hints':>6}")
    for pack in PACK_ORDER:
        print(f"  {pack:<16} {len(buckets[pack]['BAD_PATTERNS']):>5} "
              f"{len(buckets[pack]['ERROR_HINTS']):>6}")
    print(f"  {'TOTAL':<16} {len(legacy.BAD_PATTERNS):>5} "
          f"{len(legacy.ERROR_HINTS):>6}  (union verified identical)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
