"""Deterministic Dart import repair for the sovereign worker loop.

The Dart twin of `import_fixer.py`. The most expensive local-model failure is
not bad logic, it is a missing or wrong import — the model writes correct code
that references a real project symbol and simply does not know which file it
lives in. Re-prompting a 14B to regenerate an entire file in order to add one
`import` line is the most costly possible way to fix a one-line mechanical
problem.

  * build_symbol_index()  scans lib/ for every top-level declaration and records
    the `package:` URI that provides it.
  * fix_dart_imports()    adds the missing imports to a generated file, so it
    analyzes without the model ever getting the import right. Runs BEFORE
    validation, like the TS path.
  * build_import_map()    renders a compact "import these from here" block for
    the coding prompt.

Observed on Witch's Bricks 2026-08-09: three consecutive 14B attempts burned on
`The name 'Unit' isn't a type` because the task named derived/event/state but
not types.dart. Zero of those attempts were about the algorithm.

Additive by design. It only ADDS imports for symbols it can resolve
unambiguously; it never rewrites or removes an existing import line. Ambiguous
symbols (declared in two files) are skipped and left for the model.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP_DIRS = {".git", "build", ".dart_tool", ".venv", "ios", "android",
              "macos", "windows", "linux", "web", "test"}
_CODEGEN = (".g.dart", ".freezed.dart", ".gr.dart", ".mocks.dart", ".pb.dart")

# Top-level declarations only — anchored at column 0, so class members are
# excluded. A member is reached through an instance, not imported by name.
_DECL_PATTERNS = [
    re.compile(r"^(?:abstract\s+|sealed\s+|base\s+|final\s+|interface\s+|mixin\s+)*"
               r"class\s+(\w+)", re.M),
    re.compile(r"^enum\s+(\w+)", re.M),
    re.compile(r"^mixin\s+(\w+)", re.M),
    re.compile(r"^extension\s+(\w+)", re.M),
    re.compile(r"^typedef\s+(\w+)", re.M),
    re.compile(r"^(?:[\w$<>,\s?\[\]]+?\s+)(\w+)\s*\([^;]*?\)\s*(?:async\s*)?[{=]", re.M),
    re.compile(r"^(?:const|final|var)\s+(?:[\w<>,\s?\[\]]+\s+)?(\w+)\s*=", re.M),
]

_IMPORT_RE = re.compile(r"^\s*import\s+'([^']+)'[^;]*;", re.M)
_TOKEN_RE = re.compile(r"\b([A-Za-z_]\w*)\b")


def _pkg_name(project_root: str) -> str | None:
    p = os.path.join(project_root, "pubspec.yaml")
    if not os.path.exists(p):
        return None
    m = re.search(r"^name:\s*(\S+)", open(p).read(), re.M)
    return m.group(1) if m else None


def _strip(src: str) -> str:
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"'''.*?'''|\"\"\".*?\"\"\"", "''", src, flags=re.S)
    src = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", src)
    return re.sub(r'"(?:\\.|[^"\\\n])*"', '""', src)


def declared_symbols(src: str) -> set[str]:
    """Every top-level name a Dart source file declares."""
    body = _strip(src)
    out: set[str] = set()
    for rx in _DECL_PATTERNS:
        for m in rx.finditer(body):
            name = m.group(1)
            if name and name[0].isalpha() and name not in {
                    "if", "for", "while", "switch", "return", "import", "export",
                    "part", "library", "get", "set", "operator"}:
                out.add(name)
    return out


def build_symbol_index(project_root: str) -> dict[str, str]:
    """symbol -> `package:<pkg>/<path>.dart`. Ambiguous symbols are dropped."""
    pkg = _pkg_name(project_root)
    if not pkg:
        return {}
    seen: dict[str, set[str]] = {}
    lib = Path(project_root) / "lib"
    for p in lib.rglob("*.dart"):
        if any(s in p.parts for s in _SKIP_DIRS) or p.name.endswith(_CODEGEN):
            continue
        try:
            src = p.read_text(errors="ignore")
        except OSError:
            continue
        rel = p.relative_to(lib).as_posix()
        uri = f"package:{pkg}/{rel}"
        for sym in declared_symbols(src):
            seen.setdefault(sym, set()).add(uri)
    return {s: next(iter(u)) for s, u in seen.items() if len(u) == 1}


def _normalise(uri: str, rel_path: str, pkg: str | None) -> str:
    """A relative import resolves to the same library as its package: URI.

    Models write `import 'hex.dart';` as often as the package form. Comparing
    raw strings makes the fixer think the symbol is missing and add a DUPLICATE
    import of the same library, which then fails as an unused import.
    """
    if uri.startswith(("package:", "dart:")) or not pkg:
        return uri
    base = os.path.dirname(rel_path)
    joined = os.path.normpath(os.path.join(base, uri)).replace("\\", "/")
    if joined.startswith("lib/"):
        joined = joined[len("lib/"):]
    return f"package:{pkg}/{joined}"


def _provided_by_current_imports(src: str, rel_path: str, pkg: str | None,
                                 index: dict[str, str]) -> set[str]:
    uris = {_normalise(u, rel_path, pkg) for u in _IMPORT_RE.findall(src)}
    return {s for s, u in index.items() if u in uris}


def fix_dart_imports(project_root: str, files: list[str],
                     index: dict[str, str] | None = None) -> list[str]:
    """Add missing project imports to each generated file. Returns descriptions."""
    if index is None:
        index = build_symbol_index(project_root)
    if not index:
        return []
    fixes: list[str] = []
    for rel in files:
        if not rel.endswith(".dart") or rel.endswith(_CODEGEN):
            continue
        path = os.path.join(project_root, rel)
        if not os.path.exists(path):
            continue
        src = open(path).read()
        body = _strip(src)
        body = _IMPORT_RE.sub("", body)

        local = declared_symbols(src)
        have = _provided_by_current_imports(src, rel, _pkg_name(project_root), index)
        self_uri = index.get(next(iter(local)), None) if local else None

        needed: set[str] = set()
        for tok in set(_TOKEN_RE.findall(body)):
            if tok in local or tok in have or tok not in index:
                continue
            uri = index[tok]
            if self_uri and uri == self_uri:
                continue
            needed.add(uri)

        if not needed:
            continue
        lines = src.split("\n")
        last = max((i for i, l in enumerate(lines) if _IMPORT_RE.match(l)),
                   default=-1)
        insert_at = last + 1 if last >= 0 else 0
        added = [f"import '{u}';" for u in sorted(needed)]
        lines[insert_at:insert_at] = added
        open(path, "w").write("\n".join(lines))
        fixes.append(f"{rel}: added {len(added)} import(s) "
                     f"({', '.join(u.rsplit('/', 1)[-1] for u in sorted(needed))})")
    return fixes


def build_import_map(project_root: str, symbols: list[str],
                     index: dict[str, str] | None = None) -> str:
    """A compact 'import these from here' block for the coding prompt."""
    if index is None:
        index = build_symbol_index(project_root)
    by_uri: dict[str, list[str]] = {}
    for s in symbols:
        if s in index:
            by_uri.setdefault(index[s], []).append(s)
    if not by_uri:
        return ""
    out = ["IMPORTS — use exactly these, do not guess a path:"]
    for uri in sorted(by_uri):
        out.append(f"  import '{uri}';   // {', '.join(sorted(by_uri[uri]))}")
    return "\n".join(out)
