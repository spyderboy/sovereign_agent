#!/usr/bin/env python3
"""guard_selftest.py — prove the gates do not reject correct code.

    python3 guard_selftest.py ~/Code/witches_bricks

Runs every committed source file back through the grounder and the bad-pattern
guards. The committed tree ANALYZES CLEAN, so every file in it is correct by
construction: anything a gate flags is a false positive, full stop. No judgement
call, no threshold.

WHY THIS EXISTS

Five grounder false positives in three days, each found the expensive way —
by a task failing at every tier while the output was correct:

    loop variables            `for (final unitJson in raw)`      12 blocks
    single-word members       `s.hints`                          12 blocks
    top-level getters         `List<String> get profileNames`     launch blocked
    typed function params     `bool unlocked(int order, int x)`   7 blocks
    a file's own enum values  `enum SocketState { legalTarget }`  7 blocks

Every one was invisible from outside: repeated identical failures that look
exactly like a model that cannot code. Each cost a run.

`declared_names` was built by enumerating the ways Dart declares a name, and
that enumeration will always be incomplete — the next syntax will find the next
hole. The fix is not a sixth regex. It is to check the enumeration against
reality before every launch, which takes a second and finds all of them at once.

Run it after ANY change to the grounder or the guards, and from preflight.
"""

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    project = os.path.abspath(os.path.expanduser(a.project))

    files = sorted(glob.glob(os.path.join(project, "lib", "**", "*.dart"),
                             recursive=True))
    if not files:
        print("no lib/**/*.dart to check")
        return 0

    failures: list[str] = []

    # ── the grounder ─────────────────────────────────────────────────────────
    try:
        import dart_grounding as dg
        dg.invalidate_project_cache(project)
        for path in files:
            rel = os.path.relpath(path, project)
            if dg.is_codegen(rel):
                continue
            src = open(path, errors="ignore").read()
            violations = dg.check_dart_grounding(rel, src, project)
            for v in violations or []:
                failures.append(f"grounder rejects committed {rel}: {v[:120]}")
    except ImportError as e:
        print(f"(grounder not importable: {e})")

    # ── the bad-pattern guards ───────────────────────────────────────────────
    for cfg_name in (".sovereign_config.json", ".sovereign_config.sim.json",
                     ".sovereign_config.render.json"):
        cfg_path = os.path.join(project, cfg_name)
        if not os.path.exists(cfg_path):
            continue
        cfg = json.load(open(cfg_path))
        locked = tuple(cfg.get("locked_prefixes", []))
        # locked_FILES matter as much as locked_prefixes. lib/sim/types.dart is
        # locked and DECLARES the very types the redeclare guard protects, so
        # skipping only prefixes reported ten false alarms about the guard being
        # a false alarm.
        locked_files = set(cfg.get("locked_files", []))
        for entry in cfg.get("additional_bad_patterns", []):
            pat = entry["pattern"]
            for path in files:
                rel = os.path.relpath(path, project)
                # A locked layer is never regenerated under this config, so a
                # guard firing there is irrelevant, not a false positive.
                if rel.startswith(locked) or rel in locked_files:
                    continue
                try:
                    m = re.search(pat, open(path, errors="ignore").read(), re.M)
                except re.error:
                    continue
                if m:
                    failures.append(
                        f"{cfg_name} guard /{pat[:36]}/ fires on committed "
                        f"{rel}: {m.group(0)[:50]!r}")

    if failures:
        print(f"{len(failures)} GATE(S) REJECT CORRECT CODE:\n")
        for f in failures:
            print("  -", f)
        print("\n  The committed tree analyzes clean, so each of these is a false\n"
              "  positive. A model hitting one fails at every tier and looks\n"
              "  incapable. Fix the gate, not the model.")
        return 1

    if not a.quiet:
        print(f"gates clean — {len(files)} committed file(s) pass the grounder "
              f"and every bad-pattern guard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
