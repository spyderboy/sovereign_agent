#!/usr/bin/env python3
"""api_surface.py — make "do not guess APIs" a mechanism instead of a wish.

    python3 api_surface.py <project> --mine        # logs -> new guards
    python3 api_surface.py <project> --rows        # locked types -> API.md rows
    python3 api_surface.py <project> --check       # tasks naming a type must show it

WHY A TOOL AND NOT A RULE

"Do not invent APIs" is already in every prompt. It does not work, because the
model is not choosing to guess — it does not know it is guessing. It has seen a
million Flutter palettes with `.primary`, so `pal.primary` feels like recall.
Telling it to try harder changes nothing; removing the ambiguity does.

Three layers, weakest to strongest:

  rows    A cheat-sheet row naming the WRONG form beside the right one. This is
          what killed `WitchType.EARTH` in one pass after three tasks failed on
          it. Necessary but hand-authored, so it always lags the failure.

  mine    THE DEFINITIVE LAYER. An analyzer error is a machine-readable fact
          about your API: "The getter 'primary' isn't defined for the type
          'TeamPalette'" says TeamPalette has no `primary`, forever. Mine those
          out of the run logs and emit a bad-pattern guard per fact. The guard
          then fires at GENERATION time, before validation, and its hint is the
          repair prompt. Each guess costs one attempt ONCE, project-wide,
          instead of once per task that makes it.

  check   A task that names a type without showing its surface is under-specified.
          Preflight can refuse it, the way it refuses an oversized task.

The mined guards are the reason this is definitive: the set of ways to guess an
API is unbounded, but the set of guesses that ACTUALLY HAPPEN is small, finite,
and observable. You do not have to predict them, only harvest them.
"""

import argparse
import glob
import json
import os
import re
import sys

# Analyzer messages that state a fact about the API surface.
_PATTERNS = [
    (re.compile(r"The getter '(\w+)' isn't defined for the type '(\w+)'"),
     lambda m: (m.group(2), m.group(1), "getter")),
    (re.compile(r"The setter '(\w+)' isn't defined for the type '(\w+)'"),
     lambda m: (m.group(2), m.group(1), "setter")),
    (re.compile(r"The method '(\w+)' isn't defined for the type '(\w+)'"),
     lambda m: (m.group(2), m.group(1), "method")),
    (re.compile(r"There's no constant named '(\w+)' in '(\w+)'"),
     lambda m: (m.group(2), m.group(1), "constant")),
]

_DECL = re.compile(
    r"^(?:abstract |sealed |base |final )*(?:class|enum|mixin) (\w+)", re.M)


def surfaces(project: str) -> dict[str, dict]:
    """Public surface of every type declared under lib/."""
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(project, "lib", "**", "*.dart"),
                          recursive=True):
        src = open(path, errors="ignore").read()
        rel = os.path.relpath(path, project)
        for m in _DECL.finditer(src):
            name = m.group(1)
            body = src[m.end():]
            depth, end = 0, len(body)
            for i, ch in enumerate(body):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            body = body[:end]
            fields = re.findall(r"^\s*final\s+[\w<>,?\s]+\s+(\w+)\s*;", body, re.M)
            ctor = re.search(rf"^\s*const\s+{name}\(([^)]*)\)", body, re.M) or \
                re.search(rf"^\s*{name}\(([^)]*)\)", body, re.M)
            consts = re.findall(r"^\s*(\w+),?\s*$", body, re.M)
            out[name] = {
                "file": rel,
                "fields": fields,
                "ctor": (ctor.group(1).strip() if ctor else None),
                "constants": [c for c in consts if c and c[0].islower()],
            }
    return out


def mine(project: str) -> list[dict]:
    """Bad-pattern guards derived from errors the runs actually produced."""
    facts: dict[tuple, str] = {}
    logs = sorted(glob.glob(os.path.join(project, "logs", "*-work.log")))
    for lg in logs:
        text = open(lg, errors="ignore").read()
        for rx, extract in _PATTERNS:
            for m in rx.finditer(text):
                typ, member, kind = extract(m)
                facts[(typ, member)] = kind

    known = surfaces(project)
    guards = []
    for (typ, member), kind in sorted(facts.items()):
        info = known.get(typ)
        if not info:
            # AN SDK TYPE IS STILL A FACT. `There's no constant named 'add' in
            # 'BlendMode'` is exactly as permanent and as machine-readable as
            # the same sentence about one of our own classes — the analyzer has
            # settled it forever. Skipping these threw away half the harvest and
            # left gem_painter blocked on BlendMode.add through seven attempts
            # and a whole task budget (2026-08-14).
            #
            # We cannot list the real surface for an SDK type without scanning
            # it, so the hint says less. It still says the one thing that
            # matters: this member does not exist, stop reaching for it.
            guards.append({
                "pattern": rf"\b{typ}\.{member}\b",
                "hint": (f"`{typ}.{member}` does not exist — the analyzer has "
                         f"already reported it as no such {kind} on {typ}. It "
                         f"is an SDK type, so check its real surface rather "
                         f"than assuming the name from another framework. "
                         f"(dart:ui's BlendMode, for one, spells additive "
                         f"blending `plus`, not `add`.)"),
            })
            continue
        real = info["fields"] or info["constants"]
        if member in real:
            continue                      # it exists now — the error was transient
        # Prefer the BARE member: real code says `pal.auraColor`, far from any
        # mention of TeamPalette, so a type-proximity pattern almost never
        # fires. The bare form is only safe if it matches nothing already
        # committed — the tree analyzes clean, so anything in it is legitimate.
        bare = rf"\.{member}\b"
        collides = any(re.search(bare, open(f, errors="ignore").read())
                       for f in glob.glob(os.path.join(project, "lib", "**", "*.dart"),
                                          recursive=True)
                       + glob.glob(os.path.join(project, "test", "**", "*.dart"),
                                   recursive=True))
        guards.append({
            "pattern": (rf"\b{typ}\b[^;\n]{{0,40}}\.{member}\b" if collides
                        else bare),
            "hint": (f"{typ} has no {kind} '{member}'. Its real surface is: "
                     f"{', '.join(real[:10]) or '(see ' + info['file'] + ')'}. "
                     f"Defined in {info['file']} — read it rather than assuming "
                     f"the shape from other projects."),
        })
    return guards


def rows(project: str) -> list[str]:
    """Cheat-sheet rows for every locked type, straight from source."""
    cfg = json.load(open(os.path.join(project, ".sovereign_config.json")))
    locked = set(cfg.get("locked_files", []))
    prefixes = tuple(cfg.get("locked_prefixes", []))
    out = []
    for name, info in sorted(surfaces(project).items()):
        if not (info["file"] in locked or info["file"].startswith(prefixes)):
            continue
        if info["constants"]:
            out.append(f"| `{name}` values | "
                       f"{', '.join('`' + name + '.' + c + '`' for c in info['constants'][:6])} | "
                       f"UPPER_SNAKE spellings — those are the JSON encoding |")
        elif info["fields"]:
            out.append(f"| `{name}` fields | "
                       f"{', '.join('`' + f + '`' for f in info['fields'][:8])} | "
                       f"anything else; constructor is `{name}({info['ctor'] or ''})` |")
    return out


def check(project: str) -> list[str]:
    """Open tasks that name a locked type without showing its shape."""
    known = surfaces(project)
    cfg = json.load(open(os.path.join(project, ".sovereign_config.json")))
    ctx = set(cfg.get("context_always_include", []))
    problems = []
    for line in open(os.path.join(project, "ROADMAP.md")):
        if not line.startswith("- [ ]"):
            continue
        target = re.search(r"In (\S+?):", line)
        for name, info in known.items():
            if not re.search(rf"`?\b{name}\b", line):
                continue
            if info["file"] in ctx:
                continue                  # the worker sees the declaration itself
            shown = (f"{name}(" in line) or any(f"`{f}`" in line
                                                for f in info["fields"][:3])
            if not shown and (info["fields"] or info["constants"]):
                problems.append(
                    f"{target.group(1) if target else '?'} names `{name}` but "
                    f"neither shows its shape nor has {info['file']} in "
                    f"context_always_include")
    return sorted(set(problems))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--mine", action="store_true")
    ap.add_argument("--rows", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="with --mine, append the guards to the active config")
    a = ap.parse_args()
    project = os.path.abspath(os.path.expanduser(a.project))

    if a.mine:
        guards = mine(project)
        print(f"{len(guards)} guard(s) mined from run logs:\n")
        for g in guards:
            print(f"  {g['pattern']}\n      {g['hint'][:110]}\n")
        if a.apply and guards:
            cfg_path = os.path.join(project, ".sovereign_config.json")
            cfg = json.load(open(cfg_path))
            have = {p["pattern"] for p in cfg.setdefault("additional_bad_patterns", [])}
            added = [g for g in guards if g["pattern"] not in have]
            cfg["additional_bad_patterns"].extend(added)
            json.dump(cfg, open(cfg_path, "w"), indent=2)
            print(f"appended {len(added)} new guard(s) to {cfg_path}")
    if a.rows:
        for r in rows(project):
            print(r)
    if a.check:
        probs = check(project)
        for p in probs:
            print("  -", p)
        print(f"{len(probs)} under-specified task(s)")
        return 1 if probs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
