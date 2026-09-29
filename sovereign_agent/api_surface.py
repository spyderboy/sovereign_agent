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
import ast
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


# ─── Python (2026-09-28) ──────────────────────────────────────────────────────
# The tool above only knew Dart analyzer messages, so on a Python project
# --mine found nothing: xmeasure's store layer failed three passes running on
# invented names (Issue.summary, IssueSchema, get_conn, wrong table columns)
# with no guard ever produced. Python states the same facts at runtime.

_PY_PATTERNS = [
    (re.compile(r"AttributeError: '(\w+)' object has no attribute '(\w+)'"),
     lambda m: ("attribute", m.group(1), m.group(2))),
    (re.compile(r"ImportError: cannot import name '(\w+)' from '([\w.]+)'"),
     lambda m: ("import", m.group(2), m.group(1))),
    (re.compile(r"TypeError: (\w+)\.__init__\(\) got an unexpected keyword argument '(\w+)'"),
     lambda m: ("kwarg", m.group(1), m.group(2))),
    (re.compile(r"OperationalError: table (\w+) has no column named (\w+)"),
     lambda m: ("column", m.group(1), m.group(2))),
]
_PY_SKIP = {".venv", "venv", "logs", "build", "node_modules", "reference", "__pycache__",
            "site-packages", ".tox", "dist"}
_SQL_TABLE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?[\"`]?(\w+)[\"`]?\s*\(", re.I)
_SQL_NOT_COLUMN = {"primary", "unique", "foreign", "check", "constraint"}


def sql_tables(src: str) -> dict[str, list[str]]:
    """Columns of every CREATE TABLE in a source file: follow the parentheses
    and split only at top-level commas, so `PRIMARY KEY (repo, sha)` and one
    long multi-table DDL string both parse correctly."""
    out: dict[str, list[str]] = {}
    for m in _SQL_TABLE.finditer(src):
        depth, i, start, parts = 1, m.end(), m.end(), []
        while i < len(src) and depth:
            ch = src[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    parts.append(src[start:i])
            elif ch == "," and depth == 1:
                parts.append(src[start:i])
                start = i + 1
            i += 1
        cols = []
        for part in parts:
            words = part.strip().split()
            if not words:
                continue
            word = words[0].strip('"`[]')
            if word.lower() not in _SQL_NOT_COLUMN and re.fullmatch(r"\w+", word):
                cols.append(word)
        out[m.group(1)] = cols
    return out


def language(project: str) -> str:
    cfg_path = os.path.join(project, ".sovereign_config.json")
    if os.path.exists(cfg_path):
        lang = (json.load(open(cfg_path)).get("language") or "").lower()
        if lang:
            return lang
    if glob.glob(os.path.join(project, "lib", "**", "*.dart"), recursive=True):
        return "dart"
    return "python" if py_files(project, tests=False) else "unknown"


def py_files(project: str, tests: bool = True) -> list[str]:
    out = []
    for dirpath, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in _PY_SKIP and not d.startswith(".")
                   and (tests or d not in ("tests", "test"))]
        out += [os.path.join(dirpath, f) for f in files if f.endswith(".py")]
    return sorted(out)


def py_surfaces(project: str) -> tuple[dict, dict, dict]:
    """(classes, modules, tables) declared in the project's own source."""
    classes: dict[str, dict] = {}
    modules: dict[str, dict] = {}
    tables: dict[str, list[str]] = {}
    for path in py_files(project, tests=False):
        rel = os.path.relpath(path, project)
        src = open(path, errors="ignore").read()
        tables.update(sql_tables(src))
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        dotted = rel[:-3].replace(os.sep, ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names += [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
            elif isinstance(node, ast.ImportFrom):
                names += [a.asname or a.name for a in node.names]
            if isinstance(node, ast.ClassDef):
                fields, methods = [], []
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        fields.append(item.target.id)
                    elif isinstance(item, ast.Assign):
                        fields += [t.id for t in item.targets if isinstance(t, ast.Name)]
                    elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(item.name)
                bases = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
                classes[node.name] = {"file": rel, "fields": fields, "methods": methods,
                                      "enum": bool(bases & {"Enum", "IntEnum", "StrEnum"})}
        modules[dotted] = {"file": rel, "names": [n for n in names if not n.startswith("_")]}
    return classes, modules, tables


def work_logs(project: str) -> list[str]:
    return sorted(glob.glob(os.path.join(project, "logs", "*-work*.log")))


def _matches_tree(pattern: str, files: list[str]) -> bool:
    rx = re.compile(pattern, re.M)
    return any(rx.search(open(f, errors="ignore").read()) for f in files)


def mine_python(project: str) -> list[dict]:
    facts: set[tuple] = set()
    for lg in work_logs(project):
        text = open(lg, errors="ignore").read()
        for rx, extract in _PY_PATTERNS:
            for m in rx.finditer(text):
                facts.add(extract(m))
    classes, modules, tables = py_surfaces(project)
    tree_files = py_files(project, tests=True)
    guards = []
    for kind, owner, name in sorted(facts):
        if kind in ("attribute", "kwarg"):
            info = classes.get(owner)
            if not info:
                continue                    # builtins / third-party: not our contract
            real = info["fields"] + info["methods"]
            if name in real:
                continue                    # exists now — the error was transient
            if kind == "attribute":
                pattern = rf"\.{name}\b"
                if _matches_tree(pattern, tree_files):
                    pattern = rf"\b{owner}\b[^\n]{{0,40}}\.{name}\b"
                hint = (f"{owner} has no attribute '{name}'. Its fields are: "
                        f"{', '.join(info['fields'][:12]) or '(none)'}; methods: "
                        f"{', '.join(m for m in info['methods'] if not m.startswith('_'))[:120] or '(none)'}. "
                        f"Defined in {info['file']} — read it; don't assume the shape.")
            else:
                pattern = rf"\b{owner}\([^)]*\b{name}\s*="
                hint = (f"{owner}(...) takes no '{name}' argument. Its fields are: "
                        f"{', '.join(info['fields'][:12])}. Construct it with exactly "
                        f"those keywords (see {info['file']}).")
        elif kind == "import":
            info = modules.get(owner)
            if not info or name in info["names"]:
                continue
            pattern = rf"\bfrom\s+{re.escape(owner)}\s+import\b[^\n]*\b{name}\b"
            hint = (f"`{owner}` has no `{name}`. It provides: "
                    f"{', '.join(info['names'][:15]) or '(nothing public)'} "
                    f"({info['file']}). Import only names that exist.")
        elif kind == "column":
            cols = tables.get(owner)
            if not cols or name in cols:
                continue
            pattern = (rf"(?i)(?:insert\s+(?:or\s+\w+\s+)?into|update|from|join)\s+"
                       rf"{owner}\b[^;\"']{{0,300}}\b{name}\b")
            hint = (f"Table {owner} has no column '{name}'. Its columns are: "
                    f"{', '.join(cols)} (store schema). Use exactly those names.")
        else:
            continue
        if _matches_tree(pattern, [f for f in tree_files]):
            continue                        # would reject committed (correct) code
        guards.append({"pattern": pattern, "hint": hint})
    return guards


def rows_python(project: str) -> list[str]:
    cfg = json.load(open(os.path.join(project, ".sovereign_config.json")))
    locked = set(cfg.get("locked_files", []))
    prefixes = tuple(cfg.get("locked_prefixes", []))
    classes, _, tables = py_surfaces(project)
    out = []
    for name, info in sorted(classes.items()):
        if (info["file"] in locked or info["file"].startswith(prefixes)) and info["fields"]:
            if info.get("enum"):
                out.append(f"| `{name}` members | "
                           f"{', '.join('`' + name + '.' + f + '`' for f in info['fields'][:12])} | "
                           f"any other member; lowercase or string spellings |")
            else:
                out.append(f"| `{name}` fields | "
                           f"{', '.join('`' + f + '`' for f in info['fields'][:12])} | "
                           f"any other attribute; construct with these keywords only |")
    for t, cols in sorted(tables.items()):
        out.append(f"| table `{t}` columns | {', '.join('`' + c + '`' for c in cols)} | "
                   f"any other column name |")
    return out


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

    lang = language(project)
    if a.mine:
        guards = mine_python(project) if lang == "python" else mine(project)
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
        for r in (rows_python(project) if lang == "python" else rows(project)):
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
