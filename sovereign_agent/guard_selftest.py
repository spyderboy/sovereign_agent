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

    # ── guards must not contradict each other ────────────────────────────────
    # A guard's hint is a repair prompt: the model reads it and writes what it
    # says. If guard A's hint recommends a form that guard B forbids, the model
    # is trapped — blocked, told to write X, blocked again for writing X, at
    # every tier, forever. From outside it is indistinguishable from a model
    # that cannot code.
    #
    # 2026-08-13 cost an afternoon to this. Three prefix-era hints said to write
    # `Paint()..shader = ui.Gradient.linear(...)` while a newer guard blocked
    # `ui.` under a bare dart:ui import. Both were right about their own case
    # and neither knew about the other.
    #
    # Only the RECOMMENDED snippets are checked. A hint naming the wrong form to
    # warn against it is doing its job, so anything introduced by never/not/
    # instead of/rather than is skipped.
    _NEGATED = re.compile(
        r"(?:never|not|instead of|rather than|no exception|avoid|"
        r"do not|don't|wrong|deprecated)[^.]{0,60}$", re.I)
    for cfg_name in (".sovereign_config.json", ".sovereign_config.render.json",
                     ".sovereign_config.sim.json"):
        cfg_path = os.path.join(project, cfg_name)
        if not os.path.exists(cfg_path):
            continue
        guards = json.load(open(cfg_path)).get("additional_bad_patterns", [])
        for g in guards:
            for m in re.finditer(r"`([^`]+)`", g.get("hint", "")):
                if _NEGATED.search(g["hint"][:m.start()]):
                    continue
                snippet = m.group(1)
                # Test the snippet AS IT WOULD APPEAR IN A FILE, not bare. Guards
                # that span an import and its usage — `import 'dart:ui';` ... then
                # `ui.` — never match a naked fragment, so the first version of
                # this check could not see the very contradiction it was written
                # for. The default file shape in this layer is a bare dart:ui
                # import; a hint that recommends something illegal there must say
                # it is for a widget file, or it will be followed in the wrong one.
                prefix = "import 'dart:ui';\n"
                probe = prefix + snippet
                if "widget" in g.get("hint", "").lower():
                    continue
                for other in guards:
                    if other is g:
                        continue
                    try:
                        # The match must reach INTO the snippet. Without this the
                        # injected prefix is itself the violation — every hint in
                        # the sim config was flagged because that layer bans
                        # dart:ui outright, and the probe had just imported it.
                        hit = re.search(other["pattern"], probe, re.M)
                        if hit and hit.end() > len(prefix):
                            failures.append(
                                f"{cfg_name} guards CONTRADICT: a hint tells the "
                                f"model to write `{snippet[:44]}`, which guard "
                                f"/{other['pattern'][:34]}/ then blocks. It will "
                                f"loop until the ladder runs out")
                    except re.error:
                        pass

    # ── preflight's own hygiene heuristics ───────────────────────────────────
    # Preflight guesses too. Its unused-import check works from a hand-listed
    # set of identifiers per SDK library, and material re-exports painting and
    # animation — so a file holding `final Curve curve` and nothing else looked
    # like an unused import and blocked a launch (2026-08-13).
    #
    # ONLY unused-import findings are treated as false positives here. The rest
    # of check_dart_hygiene is exact rather than heuristic — a 200-line file or
    # a Flutter import inside lib/sim is a REAL violation and must keep failing.
    # An unused import cannot be real: the tree analyzes clean, and the analyzer
    # would have caught it.
    pf = os.path.join(project, "tool", "preflight.py")
    if os.path.exists(pf):
        import importlib.util
        cwd = os.getcwd()
        try:
            os.chdir(project)
            spec = importlib.util.spec_from_file_location("_pf_selftest", pf)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.FAIL.clear()
            mod.check_dart_hygiene()
            for f in mod.FAIL:
                if "unused import" in f:
                    failures.append(f"preflight calls a USED import unused: {f}")
        except Exception as e:
            print(f"(could not run preflight hygiene: {e})")
        finally:
            os.chdir(cwd)

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
