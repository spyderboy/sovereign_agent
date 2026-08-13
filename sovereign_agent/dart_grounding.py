"""
dart_grounding.py — mechanical identifier- and import-grounding gate for
Dart/Flutter projects. The Dart sibling of grounding.py.

WHY
---
Measured on GalaxicanJS/astro-flux's logs/errors.jsonl (147 failing attempts):

    146×  uri_does_not_exist          ← invented import URIs
    139×  undefined_function
     73×  undefined_identifier
     45×  undefined_class             ← invented symbols

107 of the 147 attempts (73%) consisted of NOTHING BUT errors in that family.
Every one of them is knowable before the file is written to disk. grounding.py
does exactly this for Go and drove those classes down; Dart had no equivalent,
which is why the Dart project generated the corpus above.

Design mirrors grounding.py deliberately — same violation-message shape, same
"rejection IS the repair prompt" contract, same degraded-mode fallback when the
SDK cannot be scanned. Read that module first; this one assumes its vocabulary.

ACCEPTED LIMITS
---------------
  - Import resolution is the high-value half and is exact: a package URI either
    resolves against pubspec.yaml + lib/ or it does not.
  - Identifier grounding is only as good as the whitelist. With
    .dart_tool/package_config.json present we scan the real SDK + dependency
    surface; without it we drop to degraded mode and flag only near-misses of
    project names, exactly as grounding.py does with no GOROOT.
  - Dart's `dynamic`, extension methods, and codegen output (*.g.dart) defeat
    static identifier checks. Codegen files are excluded from the gate.
  - Cost of a false positive is one extra model attempt with an explanatory
    message. Cost of a false negative is a compile error we already know how
    to catch. Bias accordingly.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from pathlib import Path

# ── Dart core libraries (the `dart:` scheme is closed, so this list is exact) ─

DART_CORE_LIBS = {
    "async", "cli", "collection", "convert", "core", "developer", "ffi", "io",
    "isolate", "js", "js_interop", "js_interop_unsafe", "js_util", "math",
    "mirrors", "typed_data", "ui", "html", "indexed_db", "svg", "web_audio",
    "web_gl", "wasm",
}

# Fallback identifier surface for degraded mode. Not exhaustive by design —
# degraded mode only flags near-misses, so this just suppresses the obvious.
SDK_IDENTS_STATIC = {
    "Widget", "StatelessWidget", "StatefulWidget", "State", "BuildContext",
    "MaterialApp", "Scaffold", "AppBar", "Text", "Container", "Column", "Row",
    "Center", "Padding", "SizedBox", "Expanded", "Flexible", "Stack", "Positioned",
    "ListView", "GridView", "SingleChildScrollView", "GestureDetector",
    "InkWell", "ElevatedButton", "TextButton", "IconButton", "Icon", "Image",
    "Navigator", "MediaQuery", "Theme", "ThemeData", "TextStyle", "EdgeInsets",
    "BoxDecoration", "BorderRadius", "Color", "Colors", "Offset", "Size", "Rect",
    "Canvas", "CustomPainter", "CustomPaint", "Paint", "Path", "Matrix4",
    "Future", "Stream", "StreamController", "Completer", "Timer", "Duration",
    "List", "Map", "Set", "Iterable", "String", "int", "double", "bool", "num",
    "Object", "Exception", "Error", "StateError", "ArgumentError", "Comparable",
    "ChangeNotifier", "ValueNotifier", "Listenable", "VoidCallback",
    "FutureBuilder", "StreamBuilder", "AnimationController", "Tween", "Curves",
    "Key", "ValueKey", "GlobalKey", "UniqueKey", "WidgetsBinding",
    "DateTime", "RegExp", "StringBuffer", "Uri", "Random", "JsonEncoder",
    "setState", "initState", "dispose", "build", "toString", "hashCode",
    "noSuchMethod", "runtimeType", "copyWith", "toJson", "fromJson",
    "removeFromParent", "addToParent", "onLoad", "onMount", "update", "render",
}

_CACHE_DIR = os.path.expanduser("~/.cache/sovereign_grounding")
_SDK_SCAN_BUDGET_S = 25.0        # hard ceiling; degrade rather than stall a run

_sdk_cache: dict[str, tuple[set[str], set[str]]] = {}   # root -> (pkgs, idents)
_project_cache: dict[str, tuple[float, set[str]]] = {}
_pubspec_cache: dict[str, tuple[float, tuple[str | None, set[str]]]] = {}
_PROJECT_CACHE_TTL = 30.0

_CAP_TOKEN = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
# lowerCamelCase with at least one hump — Dart methods and fields. Plain
# lowercase words are excluded: they would admit every English comment word.
_LOWER_CAMEL = re.compile(r"\b([a-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+)\b")
# A single lowercase word with no hump matches NEITHER of the two patterns
# above, so every such member was invisible to the whitelist and any use of it
# was rejected as hallucinated. `s.hints` was blocked twelve times running
# against a Scenario that has a `hints` field (2026-08-12); `coord`, `text`,
# `tiles`, `units`, `shape`, `order` and `fair` were all equally unreachable.
#
# These two patterns stay narrow on purpose. Whitelisting every lowercase word
# in the tree would include prose from comments and gut the check; a name that
# is DECLARED as a field or ACCESSED through a dot somewhere in a tree that
# analyzes clean is a real member by construction.
_FIELD_DECL = re.compile(
    r"^\s*(?:static\s+|final\s+|const\s+|late\s+|var\s+|covariant\s+)*"
    r"[\w<>,\s?\[\]]*?\b([a-z]\w*)\s*[;=]", re.MULTILINE)
_DOTTED_MEMBER = re.compile(r"\.([a-z]\w*)\b")

_IMPORT_RE = re.compile(
    r"^\s*(import|export|part)\s+['\"]([^'\"]+)['\"]", re.MULTILINE
)
_PART_OF_RE = re.compile(r"^\s*part\s+of\b", re.MULTILINE)

# Declarations a file makes for itself.
_DECL_TYPE = re.compile(
    r"^\s*(?:abstract\s+|sealed\s+|base\s+|final\s+|interface\s+)*"
    r"(?:class|mixin|enum|extension|typedef)\s+([A-Za-z_]\w*)", re.MULTILINE)
_DECL_TOP_FUNC = re.compile(
    r"^\s*(?:[\w<>,\s\[\]?]+\s+)?([a-z_]\w*)\s*\([^)]*\)\s*(?:async\s*)?\{",
    re.MULTILINE)
_DECL_MEMBER = re.compile(
    r"^\s+(?:static\s+|final\s+|const\s+|late\s+|covariant\s+)*"
    r"(?:[\w<>,\s\[\]?]+\s+)?([A-Za-z_]\w*)\s*[=;(]", re.MULTILINE)
_DECL_PARAM = re.compile(r"(?:required\s+)?(?:this\.)([A-Za-z_]\w*)")

# Names bound INSIDE a function body. Without these the grounder reports a
# model's own local variables as hallucinated identifiers (2026-08-11:
# scenario/load.dart, a pure JSON parser, was blocked on `unitJson`, `uJson`
# and `playerBUnits` — its own loop variables — on every attempt at every
# tier, so escalation could never help and no model could ever pass).
_DECL_LOCAL = re.compile(
    r"\b(?:final|const|var|late)\s+(?:[\w<>,\s\[\]?]+\s+)?([A-Za-z_]\w*)\s*[=;]")
_DECL_FORIN = re.compile(
    r"\bfor\s*\(\s*(?:final|const|var)?\s*(?:[\w<>,\s\[\]?]+\s+)?"
    r"([A-Za-z_]\w*)\s+in\b")
_DECL_FORC = re.compile(
    r"\bfor\s*\(\s*(?:final|var|int|num|double)\s+([A-Za-z_]\w*)\s*=")
_DECL_CATCH = re.compile(
    r"\bcatch\s*\(\s*([A-Za-z_]\w*)\s*(?:,\s*([A-Za-z_]\w*)\s*)?\)")
# Closure parameters: `(a, b) =>`, `(a) {`, and `.map((e) => …)`.
# Any parameter list belonging to a declaration or closure: `(a, b) =>`,
# `(a) {`, `.map((e) => …)`, and `bool unlocked(int order, int done) {`.
_DECL_PARAMS = re.compile(r"\(([^()]*)\)\s*(?:async\s*)?(?:=>|\{)")
# `if (x case final Foo y)` and `case final Foo y:` pattern bindings.
_DECL_PATTERN = re.compile(
    r"\bcase\s+(?:final\s+|const\s+)?(?:[\w<>,\s\[\]?]+\s+)?([A-Za-z_]\w*)\s*[:)]")

# An enum's own CONSTANTS. `_DECL_TYPE` captures `SocketState` and stops, so a
# file that declares `enum SocketState { empty, legalTarget, selected }` and
# then writes `SocketState.legalTarget` had its own constant rejected as
# hallucinated — and the task text was what told it to declare them
# (2026-08-12, socket.dart, blocked at every tier).
_DECL_ENUM_BODY = re.compile(
    r"\benum\s+[A-Za-z_]\w*\s*\{([^}]*)\}", re.DOTALL)

_SELECTOR = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)")

_SKIP_DIRS = {".git", "build", "node_modules", "logs", ".dart_tool", ".venv",
              "ios", "android", "macos", "windows", "linux", "web"}
_CODEGEN_SUFFIXES = (".g.dart", ".freezed.dart", ".gr.dart", ".mocks.dart",
                     ".config.dart", ".pb.dart")


def is_codegen(rel_path: str) -> bool:
    return rel_path.endswith(_CODEGEN_SUFFIXES)


# ── pubspec ──────────────────────────────────────────────────────────────────

def pubspec_info(project_root: str) -> tuple[str | None, set[str]]:
    """Return (package name, declared dependency names) from pubspec.yaml.

    Hand-parsed rather than via PyYAML: the agent's venv should not need a new
    dependency to run a gate, and we only need two top-level keys.
    """
    cached = _pubspec_cache.get(project_root)
    if cached and time.time() - cached[0] < _PROJECT_CACHE_TTL:
        return cached[1]

    path = os.path.join(project_root, "pubspec.yaml")
    name: str | None = None
    deps: set[str] = set()
    if os.path.exists(path):
        section = None
        try:
            for raw in open(path, errors="ignore"):
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if not line[:1].isspace():                    # top-level key
                    key = line.split(":", 1)[0].strip()
                    section = key if key in (
                        "dependencies", "dev_dependencies",
                        "dependency_overrides") else None
                    if key == "name":
                        name = line.split(":", 1)[1].strip().strip("'\"") or None
                    continue
                if section and re.match(r"^\s{2}[A-Za-z_]", line):
                    deps.add(line.split(":", 1)[0].strip())
        except OSError:
            pass
    # The SDK packages are always importable in a Flutter app that declares
    # the flutter dependency; sdk: flutter entries are nested and already
    # captured above by their key name.
    result = (name, deps)
    _pubspec_cache[project_root] = (time.time(), result)
    return result


def preflight(project_root: str) -> list[str]:
    """One-time environment checks. Returns human-facing problem descriptions.

    Motivated by the real corpus: 76 ledger rows on a project where
    `package:flutter/material.dart` did not resolve. No model can fix that —
    it is an unbuilt dependency tree, and every attempt was doomed before the
    first token. Detecting it costs one stat() and saves an entire run.
    """
    problems: list[str] = []
    if not os.path.exists(os.path.join(project_root, "pubspec.yaml")):
        problems.append("no pubspec.yaml at project root — not a Dart package")
        return problems
    name, deps = pubspec_info(project_root)
    if not name:
        problems.append("pubspec.yaml has no `name:` — package: URIs cannot resolve")
    if not os.path.exists(os.path.join(project_root, ".dart_tool",
                                       "package_config.json")):
        problems.append(
            "no .dart_tool/package_config.json — `dart pub get` / `flutter pub get` "
            "has not been run, so EVERY package: import will fail to resolve "
            "regardless of what the model writes")
    return problems


# ── Whitelists ───────────────────────────────────────────────────────────────

def _package_roots(project_root: str) -> dict[str, str]:
    """package name -> absolute lib/ dir, from .dart_tool/package_config.json."""
    cfg = os.path.join(project_root, ".dart_tool", "package_config.json")
    out: dict[str, str] = {}
    try:
        with open(cfg) as f:
            data = json.load(f)
        for pkg in data.get("packages", []):
            name, root = pkg.get("name"), pkg.get("rootUri", "")
            if not name or not root:
                continue
            if root.startswith("file://"):
                root = root[len("file://"):]
            if not os.path.isabs(root):
                root = os.path.normpath(os.path.join(
                    project_root, ".dart_tool", root))
            out[name] = os.path.join(root, pkg.get("packageUri", "lib/").rstrip("/"))
    except Exception:
        pass
    return out


_pkg_libs_cache: dict[str, list[str]] = {}


def _package_libraries(lib_dir: str) -> list[str]:
    """Public library files a package exposes, as package:-relative paths.

    Only the top level plus src/ are listed — that is what `package:x/y.dart`
    can address, and it keeps did-you-mean suggestions readable.
    """
    if lib_dir in _pkg_libs_cache:
        return _pkg_libs_cache[lib_dir]
    out: list[str] = []
    try:
        for p in Path(lib_dir).rglob("*.dart"):
            rel = os.path.relpath(p, lib_dir).replace("\\", "/")
            if rel.count("/") <= 1:
                out.append(rel)
    except Exception:
        pass
    _pkg_libs_cache[lib_dir] = sorted(out)
    return _pkg_libs_cache[lib_dir]


def sdk_whitelist(project_root: str) -> tuple[set[str], set[str]]:
    """(resolvable package names, identifiers) from the dependency tree.

    The Dart analogue of grounding.stdlib_whitelist(). Empty identifier set
    means "could not scan" and puts the caller in degraded mode.
    """
    if project_root in _sdk_cache:
        return _sdk_cache[project_root]

    roots = _package_roots(project_root)
    pkgs = set(roots)
    idents: set[str] = set()

    if roots:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        stamp = str(sorted(roots.items()))
        cache_file = os.path.join(
            _CACHE_DIR, f"dart_{abs(hash(stamp)) & 0xffffffff:x}.json")
        try:
            if os.path.exists(cache_file):
                with open(cache_file) as f:
                    idents = set(json.load(f))
        except Exception:
            idents = set()

        if not idents:
            deadline = time.time() + _SDK_SCAN_BUDGET_S
            for lib_dir in roots.values():
                if time.time() > deadline:
                    break
                try:
                    for p in Path(lib_dir).rglob("*.dart"):
                        if time.time() > deadline:
                            break
                        try:
                            src = p.read_text(errors="ignore")
                        except OSError:
                            continue
                        idents |= set(_CAP_TOKEN.findall(src))
                        idents |= set(_LOWER_CAMEL.findall(src))
                except Exception:
                    continue
            if idents:
                try:
                    with open(cache_file, "w") as f:
                        json.dump(sorted(idents), f)
                except OSError:
                    pass

    _sdk_cache[project_root] = (pkgs, idents)
    return pkgs, idents


def project_whitelist(project_root: str) -> set[str]:
    """Every capitalized or humped-lowerCamel token in the project's .dart files.

    Same permissive rationale as grounding.project_whitelist: the committed
    tree analyzes clean, so anything in it is a real name (or a comment word —
    harmless surplus). A name appearing NOWHERE cannot legitimately be called.
    """
    cached = _project_cache.get(project_root)
    if cached and time.time() - cached[0] < _PROJECT_CACHE_TTL:
        return cached[1]
    tokens: set[str] = set()
    for p in Path(project_root).rglob("*.dart"):
        if any(s in p.parts for s in _SKIP_DIRS):
            continue
        try:
            src = p.read_text(errors="ignore")
        except OSError:
            continue
        tokens |= set(_CAP_TOKEN.findall(src))
        tokens |= set(_LOWER_CAMEL.findall(src))
        tokens |= set(_FIELD_DECL.findall(src))
        tokens |= set(_DOTTED_MEMBER.findall(src))
    _project_cache[project_root] = (time.time(), tokens)
    return tokens


def invalidate_project_cache(project_root: str) -> None:
    _project_cache.pop(project_root, None)
    _pubspec_cache.pop(project_root, None)


def declared_names(content: str) -> set[str]:
    """Names a generated file legitimately declares itself."""
    names: set[str] = set()
    for rx in (_DECL_TYPE, _DECL_TOP_FUNC, _DECL_MEMBER, _DECL_PARAM,
               _DECL_LOCAL, _DECL_FORIN, _DECL_FORC, _DECL_CATCH,
               _DECL_PATTERN):
        for m in rx.finditer(content):
            for g in m.groups():
                if g:
                    names.add(g)
    # Parameter lists, of closures AND of declared functions. Splitting on
    # commas and accepting only bare identifiers loses every TYPED parameter:
    # `bool unlocked(int order, int highestCompleted)` yielded nothing, so the
    # function's own arguments were reported as hallucinated and no model could
    # ever write it (2026-08-12). Take the last identifier of each part, after
    # stripping `required`, braces, brackets and any default value.
    for m in _DECL_ENUM_BODY.finditer(content):
        for part in m.group(1).split(","):
            part = part.split("(")[0].strip()
            if part.isidentifier():
                names.add(part)

    for m in _DECL_PARAMS.finditer(content):
        for part in m.group(1).split(","):
            part = part.split("=")[0]
            part = part.strip(" \t\n{}[]")
            part = re.sub(r"^\s*required\s+", "", part)
            tokens = re.findall(r"[A-Za-z_]\w*", part)
            if tokens:
                names.add(tokens[-1])
    return names


# ── Import resolution ────────────────────────────────────────────────────────

def _strip_strings_and_comments(content: str) -> str:
    content = re.sub(r"//[^\n]*", "", content)
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    content = re.sub(r"'''.*?'''|\"\"\".*?\"\"\"", "''", content, flags=re.DOTALL)
    content = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", content)
    content = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', content)
    return content


def _resolve_import(uri: str, rel_path: str, project_root: str,
                    pkg_name: str | None, deps: set[str],
                    sdk_pkgs: set[str], extra_files: set[str],
                    pkg_roots: dict[str, str] | None = None) -> str | None:
    """Return None if the URI resolves, else a reason string."""
    if uri.startswith("dart:"):
        lib = uri[5:].split("/")[0]
        if lib in DART_CORE_LIBS:
            return None
        return (f"`dart:{lib}` is not a Dart core library. Core libraries are: "
                f"{', '.join(sorted(list(DART_CORE_LIBS)[:12]))}, …")

    if uri.startswith("package:"):
        rest = uri[len("package:"):]
        pkg, _, sub = rest.partition("/")
        if pkg_name and pkg == pkg_name:
            # Our own package — resolvable against lib/ on disk, exactly.
            target = os.path.join(project_root, "lib", sub)
            if os.path.exists(target) or _norm(f"lib/{sub}") in extra_files:
                return None
            return (f"`{uri}` points at lib/{sub}, which does not exist in this "
                    f"project. Import only files that are already on disk.")
        if pkg in deps or pkg in sdk_pkgs:
            # Third-party. Declaring the dependency is not enough: models
            # invent sub-libraries of REAL packages —
            # `package:flame/math_engine.dart`,
            # `package:flutter_riverpod/flutter_river.dart`. Two hand-written
            # hints.py patterns existed solely to catch that. When the tree is
            # resolved we know the package's real lib/ dir, so check the file.
            lib_dir = (pkg_roots or {}).get(pkg)
            if lib_dir and sub and os.path.isdir(lib_dir):
                if os.path.exists(os.path.join(lib_dir, sub)):
                    return None
                near = difflib.get_close_matches(
                    sub, _package_libraries(lib_dir), n=3, cutoff=0.6)
                hint = f" Did you mean: {', '.join(near)}?" if near else ""
                return (f"`{uri}` — package `{pkg}` exists, but it exports no "
                        f"library `{sub}`. Do NOT invent sub-libraries of real "
                        f"packages.{hint}")
            # Unresolved tree: a declared dependency is a legitimate import and
            # we have nothing to check the path against.
            return None
        return (f"`{uri}` refers to package `{pkg}`, which is not this project "
                f"(`{pkg_name or 'unknown'}`) and is not declared in "
                f"pubspec.yaml. Do NOT invent packages, and do NOT add "
                f"dependencies — pubspec.yaml is locked.")

    if uri.endswith(".dart"):
        base = os.path.dirname(rel_path)
        target = os.path.normpath(os.path.join(base, uri))
        if os.path.exists(os.path.join(project_root, target)):
            return None
        if _norm(target) in extra_files:
            return None
        return (f"relative import `{uri}` resolves to {target}, which does not "
                f"exist. Check the path, or import the symbol from where it is "
                f"actually defined.")

    return None   # conditional imports / unknown schemes: not our business


def _norm(p: str) -> str:
    return os.path.normpath(p).replace("\\", "/")


# ── Public gate ──────────────────────────────────────────────────────────────

def check_dart_grounding(rel_path: str, content: str, project_root: str,
                         extra_declared: set[str] | None = None,
                         extra_files: set[str] | None = None) -> list[str]:
    """Return violation messages (empty list = grounded). Never raises."""
    try:
        return _check(rel_path, content, project_root,
                      extra_declared or set(),
                      {_norm(f) for f in (extra_files or set())})
    except Exception as e:   # the gate must never kill a run
        print(f"  (dart grounding gate error, skipping: {e})")
        return []


def _check(rel_path: str, content: str, project_root: str,
           extra_declared: set[str], extra_files: set[str]) -> list[str]:
    if is_codegen(rel_path):
        return []

    violations: list[str] = []
    pkg_name, deps = pubspec_info(project_root)
    sdk_pkgs, sdk_idents = sdk_whitelist(project_root)
    pkg_roots = _package_roots(project_root)
    degraded = not sdk_idents

    # ── Imports (the 146× error code) ────────────────────────────────────
    import_prefixes: set[str] = set()
    for m in _IMPORT_RE.finditer(content):
        uri = m.group(2)
        reason = _resolve_import(uri, rel_path, project_root, pkg_name, deps,
                                 sdk_pkgs, extra_files, pkg_roots)
        if reason:
            violations.append(f"UNKNOWN IMPORT [{rel_path}]: {reason}")
        else:
            import_prefixes.add(uri.rsplit("/", 1)[-1].removesuffix(".dart"))

    # A `part of` file inherits its library's imports and declarations; we
    # cannot see them, so identifier checking would be pure noise.
    if _PART_OF_RE.search(content):
        return violations

    # ── Identifiers ──────────────────────────────────────────────────────
    proj = project_whitelist(project_root)
    declared = declared_names(content) | extra_declared
    known = proj | declared | sdk_idents | SDK_IDENTS_STATIC | import_prefixes
    body = _strip_strings_and_comments(content)

    unknown: dict[str, str] = {}
    for m in _SELECTOR.finditer(body):
        base, sel = m.group(1), m.group(2)
        if sel in known or base in known:
            continue
        if degraded and not _near_miss(sel, proj):
            continue
        unknown.setdefault(sel, f"{base}.{sel}")

    for tok in set(_CAP_TOKEN.findall(body)) | set(_LOWER_CAMEL.findall(body)):
        if tok in known or tok in unknown:
            continue
        if degraded and not _near_miss(tok, proj):
            continue
        unknown.setdefault(tok, tok)

    for sel, expr in sorted(unknown.items()):
        close = difflib.get_close_matches(sel, sorted(proj), n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        violations.append(
            f"UNKNOWN IDENTIFIER [{rel_path}]: `{expr}` — `{sel}` is not defined "
            f"anywhere in this project, its dependencies, or the Dart SDK. Do NOT "
            f"invent classes, methods, fields, or constructors.{hint} Use only "
            f"the APIs that already exist in the files you were given."
        )
    return violations


def _near_miss(name: str, proj: set[str]) -> bool:
    """Degraded-mode filter: without an SDK scan, only flag names that look
    like typos of real project names. Same policy as grounding.py with no
    GOROOT — a quiet gate beats a gate that cries wolf."""
    if difflib.get_close_matches(name, proj, n=1, cutoff=0.75):
        return True
    return name.lower() in {p.lower() for p in proj}
