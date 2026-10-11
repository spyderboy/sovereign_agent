#!/usr/bin/env python3
"""kickoff.py — prepare a project so its first sweep doesn't pay for every
mistake the hard way.

    python3 kickoff.py ~/Code/<project>                    # report only
    python3 kickoff.py ~/Code/<project> --apply --commit   # write config + commit

Run it after the kit (spec, locked contract, tests, ROADMAP, config) exists
and before the first sweep. It never touches a project with a live run.

WHY

Xanadu learns by failing: a guessed API becomes a guard, a repeated mistake a
rule, a layer's first merges the examples later tasks copy. A mature project
has all of that; a new one pays full price for each mistake the first time.
On 2026-09-28 xmeasure's store layer failed three passes running — on names
the models invented although model.py was in their context — and the fast
pass spent 90 minutes a pass retrying tasks it had already failed. Most of
what a mature project accumulates can be put in place before the first run:

  1. seed     guards from earlier projects in the same language that transfer
              (no project-specific type names, and none that would reject this
              project's committed code)
  2. rows     cheat-sheet rows generated from the locked contract — fields,
              table columns — written to docs/API_ROWS.md and added to
              context_always_include
  3. check    tasks that name a locked type without showing its shape
  4. routing  a policy (sov/routing.py): each new layer's first tasks and any
              task small models keep failing go to the strong pass

After every run, keep harvesting: `python3 api_surface.py <project> --mine --apply`.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys

import api_surface
import monitor

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROUTING = {"strong_min_params_b": 25, "cold_start_tasks_per_layer": 2,
                   "route_after_failures": 2, "park_after_strong_failures": 3}
ROWS_FILE = os.path.join("docs", "API_ROWS.md")


def other_projects(project: str) -> list[str]:
    code = os.path.dirname(project)
    return sorted(p for p in glob.glob(os.path.join(code, "*"))
                  if os.path.isfile(os.path.join(p, ".sovereign_config.json"))
                  and os.path.realpath(p) != os.path.realpath(project))


def own_names(project: str, lang: str) -> set[str]:
    if lang == "python":
        classes, modules, _ = api_surface.py_surfaces(project)
        return set(classes) | {n for m in modules.values() for n in m["names"]}
    return set(api_surface.surfaces(project))


def tree_files(project: str, lang: str) -> list[str]:
    if lang == "python":
        return api_surface.py_files(project, tests=True)
    return glob.glob(os.path.join(project, "**", "*.dart"), recursive=True)


def transferable(guard: dict, names: set[str], files: list[str]) -> bool:
    """A guard transfers when it names no type this project doesn't have and
    matches none of this project's committed (correct) code."""
    pat = guard.get("pattern", "")
    if guard.get("except_paths") or guard.get("except_path"):
        return False                      # tied to another project's files
    bare = re.sub(r"\\[A-Za-z]", " ", pat)          # drop regex escapes: \b, \w, \s …
    idents = set(re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", bare))
    if any(i not in names for i in idents):
        return False
    # A mined guard keeps the type in its HINT, not its pattern: `\.light\b`
    # with "TeamPalette has no getter 'light'" seeded into a project with no
    # TeamPalette and rejected ThemeMode.light there.
    hint = guard.get("hint", "")
    subjects = set(re.findall(r"\b([A-Z]\w+) has no\b", hint))
    subjects |= set(re.findall(r"\bin '([A-Z]\w+)'", hint))
    if any(t not in names for t in subjects):
        return False
    try:
        rx = re.compile(pat, re.M)
    except re.error:
        return False
    return not any(rx.search(open(f, errors="ignore").read()) for f in files)


def seed(project: str, lang: str, cfg: dict) -> list[dict]:
    have = {g.get("pattern") for g in cfg.get("additional_bad_patterns", [])}
    names, files = own_names(project, lang), tree_files(project, lang)
    out, seen = [], set(have)
    for other in other_projects(project):
        if api_surface.language(other) != lang:
            continue
        try:
            guards = json.load(open(os.path.join(other, ".sovereign_config.json"))) \
                .get("additional_bad_patterns", [])
        except (OSError, ValueError):
            continue
        for g in guards:
            if not isinstance(g, dict) or g.get("pattern") in seen:
                continue
            if transferable(g, names, files):
                seen.add(g["pattern"])
                out.append({"pattern": g["pattern"], "hint": g["hint"],
                            "seeded_from": os.path.basename(other)})
    return out


def check_python(project: str, cfg: dict) -> list[str]:
    classes, _, _ = api_surface.py_surfaces(project)
    locked = set(cfg.get("locked_files", []))
    prefixes = tuple(cfg.get("locked_prefixes", []))
    ctx = set(cfg.get("context_always_include", []))
    problems = []
    for line in open(os.path.join(project, "ROADMAP.md"), errors="ignore"):
        if not line.startswith("- [ ]"):
            continue
        target = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", line)
        for name, info in classes.items():
            if not (info["file"] in locked or info["file"].startswith(prefixes)):
                continue
            if not info["fields"] or not re.search(rf"\b{name}\b", line):
                continue
            if info["file"] in ctx or ROWS_FILE in ctx:
                continue
            if f"{name}(" in line or any(f"`{f}`" in line or f" {f}" in line
                                         for f in info["fields"][:3]):
                continue
            problems.append(f"{target.group(1) if target else '?'} names `{name}` "
                            f"without its fields, and {info['file']} is not in context")
    return sorted(set(problems))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("--apply", action="store_true", help="write the config and rows file")
    ap.add_argument("--commit", action="store_true", help="with --apply, commit the changes")
    a = ap.parse_args()
    project = os.path.abspath(os.path.expanduser(a.project))
    cfg_path = os.path.join(project, ".sovereign_config.json")
    if not os.path.exists(cfg_path):
        print(f"no .sovereign_config.json in {project} — run init_project.py first")
        return 1
    if a.apply and monitor.run_pids(project):
        print("refusing: a run is live on this project — its workers reset the tree "
              "between tasks. Stop it first.")
        return 1
    cfg = json.load(open(cfg_path))
    lang = api_surface.language(project)
    print(f"kickoff · {os.path.basename(project)} · language {lang}\n")

    seeded = seed(project, lang, cfg)
    print(f"1. seed: {len(seeded)} transferable guard(s) from earlier {lang} projects")
    for g in seeded[:12]:
        print(f"     [{g['seeded_from']}] {g['pattern'][:70]}")

    rows = api_surface.rows_python(project) if lang == "python" else api_surface.rows(project)
    print(f"\n2. rows: {len(rows)} cheat-sheet row(s) from the locked contract → {ROWS_FILE}")
    for r in rows[:6]:
        print(f"     {r[:110]}")

    problems = check_python(project, cfg) if lang == "python" else api_surface.check(project)
    if rows:
        problems = [p for p in problems]   # rows file will be in context after --apply
    print(f"\n3. check: {len(problems)} task(s) name a locked type without showing it"
          + (" (resolved by the rows file once applied)" if rows and problems else ""))
    for p in problems[:8]:
        print(f"     {p}")

    has_routing = isinstance(cfg.get("routing"), dict)
    print(f"\n4. routing: {'already set' if has_routing else 'will add'} "
          f"{json.dumps(cfg.get('routing') or DEFAULT_ROUTING)}")

    if not a.apply:
        print("\nreport only — rerun with --apply --commit to write these.")
        return 0

    cfg.setdefault("additional_bad_patterns", []).extend(
        {"pattern": g["pattern"], "hint": g["hint"]} for g in seeded)
    if rows:
        os.makedirs(os.path.join(project, "docs"), exist_ok=True)
        with open(os.path.join(project, ROWS_FILE), "w") as f:
            f.write("# API rows — generated by kickoff.py from the locked contract\n\n"
                    "Use exactly these names. Regenerate: "
                    "`python3 api_surface.py <project> --rows`.\n\n"
                    "| What | Real names | Not |\n|---|---|---|\n" + "\n".join(rows) + "\n")
        ctx = cfg.setdefault("context_always_include", [])
        if ROWS_FILE not in ctx:
            ctx.append(ROWS_FILE)
    if not has_routing:
        cfg["routing"] = dict(DEFAULT_ROUTING)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"\nwrote {cfg_path}" + (f" and {ROWS_FILE}" if rows else ""))
    if a.commit:
        paths = [".sovereign_config.json"] + ([ROWS_FILE] if rows else [])
        subprocess.run(["git", "add", *paths], cwd=project, check=True)
        r = subprocess.run(["git", "commit", "-q", "-m",
                            "kickoff: seed guards, API rows, routing policy"], cwd=project)
        print("committed" if r.returncode == 0 else "nothing to commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
