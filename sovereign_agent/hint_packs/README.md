# hint_packs

Per-project pattern gates, split out of the old monolithic `hints.py`
(2026-07-25) by `tools/split_hints.py`. Loaded by `hints.load_packs()`,
selected from `.sovereign_config.json`.

## Packs

| pack | bad patterns | error hints | load when |
|---|---:|---:|---|
| `dart_core` | 1 | 14 | any Dart project |
| `flutter_ui` | 3 | 3 | any Flutter app |
| `dart_riverpod` | 1 | 7 | project uses Riverpod |
| `dart_flame` | 12 | 19 | project uses the Flame engine |
| `galaxican` | 7 | 42 | Galaxican / astro-flux only |
| **total** | **24** | **85** | (union verified identical to pre-split) |

45% of the original file was Galaxican-specific and was being applied to every
project. The clearest example, previously in the shared list:

> Firebase packages are NOT in this project. Do not import any firebase_*
> package. This project uses LocalPersistenceService (in-memory).

Pointed at a Firebase-backed codebase, that instructs the model to delete
correct imports on sight — and before the split there was no way to disable it.

## Selecting packs

```jsonc
{
  "language": "dart",
  "hint_packs": ["dart_core", "flutter_ui", "dart_riverpod"],
  "disable_bad_patterns": ["withOpacity"],
  "additional_bad_patterns": [
    { "pattern": "\\bLegacyApi\\b", "hint": "LegacyApi was removed in v3." }
  ]
}
```

Omit `hint_packs` and the language defaults load (`hints.DEFAULT_PACKS`):
Dart gets `dart_core` + `flutter_ui`; every other language gets none, because
every pattern in this directory is currently Dart.

## Migrating an existing project

Projects created before the split have no `hint_packs` key and will load
**fewer** patterns than they used to. To keep the old behaviour exactly, add:

```jsonc
"hint_packs": ["dart_core", "flutter_ui", "dart_riverpod", "dart_flame", "galaxican"]
```

`work.py` prints which packs loaded and which were skipped on every run, so a
silent reduction is visible rather than inferred.

## Adding a trap

Put it in the narrowest pack that could ever need it. A missing hint costs one
attempt; a wrong hint costs every attempt on every project that inherits it.

Project-specific traps do not belong here at all — use
`additional_bad_patterns` in that project's `.sovereign_config.json`.
