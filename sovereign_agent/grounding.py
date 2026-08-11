"""
grounding.py — mechanical identifier-grounding gate for Go projects.

Purpose: local models keep inventing APIs (g.FindSquad, .IsIdle, .Faction,
whole vec2 packages). Patching .roorules one hallucination at a time can't
keep up. This gate rejects a generated file BEFORE it is written to disk if
it references exported identifiers or import paths that exist nowhere in:

  1. the project's own committed source (every capitalized token in .go files
     on disk — the tree is force-cleaned to main at task start, so disk is
     ground truth),
  2. the Go standard library (scanned from GOROOT/src when available, cached;
     falls back to a static common-identifier set),
  3. the generated file(s) themselves (names they legitimately declare).

Violations come back as messages with did-you-mean suggestions, which
work.py feeds to the model on the next attempt — so rejection IS the
repair prompt.

Design notes / accepted limits:
  - Only EXPORTED selectors (x.Foo) are checked. Every observed hallucination
    was exported; unexported ones are caught by the compiler anyway.
  - Composite-literal field misuse (MaxHP: in a Squad{}) is NOT caught here —
    MaxHP is a real project identifier (a method). The compiler catches it;
    the failure ledger categorizes it.
  - Cost of a false positive is one extra model attempt with an explanatory
    message, not a lost task.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import time
from pathlib import Path

# ── Static fallbacks (used only when GOROOT can't be scanned) ────────────────

STDLIB_PACKAGES_STATIC = {
    "bufio", "bytes", "cmp", "container/heap", "container/list", "container/ring",
    "context", "encoding/binary", "encoding/json", "errors", "flag", "fmt",
    "hash/fnv", "io", "log", "maps", "math", "math/bits", "math/rand",
    "math/rand/v2", "os", "path", "path/filepath", "reflect", "regexp",
    "runtime", "slices", "sort", "strconv", "strings", "sync", "sync/atomic",
    "time", "unicode", "unicode/utf8",
}

STDLIB_IDENTS_STATIC = {
    # frequent funcs/types/methods a game sim actually touches
    "Sprintf", "Printf", "Println", "Errorf", "Fprintf", "Sprint", "Print",
    "New", "Newf", "Is", "As", "Unwrap", "Error", "String", "GoString",
    "Sqrt", "Abs", "Min", "Max", "Floor", "Ceil", "Pow", "Hypot", "Atan2",
    "Sin", "Cos", "Mod", "Inf", "NaN", "IsNaN", "IsInf", "MaxInt", "MinInt",
    "MaxInt64", "MaxFloat64", "Pi", "Round", "Trunc", "Cbrt", "Log", "Log2",
    "Intn", "Int63", "Float64", "Float32", "Perm", "Shuffle", "Seed",
    "NewSource", "Sort", "Slice", "SliceStable", "Search", "SearchInts",
    "Ints", "Strings", "Contains", "ContainsFunc", "Index", "IndexFunc",
    "SortFunc", "BinarySearch", "Clone", "Delete", "Insert", "Reverse",
    "Keys", "Values", "Equal", "EqualFold", "HasPrefix", "HasSuffix",
    "Join", "Split", "SplitN", "Fields", "TrimSpace", "Trim", "TrimPrefix",
    "TrimSuffix", "Replace", "ReplaceAll", "ToLower", "ToUpper", "Repeat",
    "Builder", "WriteString", "WriteByte", "WriteRune", "Len", "Cap", "Grow",
    "Itoa", "Atoi", "FormatInt", "FormatFloat", "ParseInt", "ParseFloat",
    "ParseBool", "Quote", "Unquote",
    "Now", "Since", "Duration", "Time", "Second", "Millisecond", "Minute",
    "Hour", "Nanosecond", "Microsecond", "Unix", "UnixNano", "UnixMilli",
    "Add", "Sub", "Before", "After", "Truncate", "Seconds", "Milliseconds",
    "Mutex", "RWMutex", "WaitGroup", "Once", "Lock", "Unlock", "RLock",
    "RUnlock", "Wait", "Done", "Do",
    "Marshal", "Unmarshal", "MarshalIndent", "NewEncoder", "NewDecoder",
    "Encode", "Decode", "Valid", "RawMessage",
    "Reader", "Writer", "ReadWriter", "EOF", "Copy", "ReadAll", "WriteTo",
    "MustCompile", "Compile", "MatchString", "FindStringSubmatch",
    "FindAllString", "FindAllStringSubmatch",
    "Getenv", "Setenv", "Exit", "Args", "Stdout", "Stderr", "Stdin",
    "Open", "Create", "ReadFile", "WriteFile", "Remove", "MkdirAll",
    "Fatal", "Fatalf", "Panic", "Panicf",
}

_CACHE_DIR = os.path.expanduser("~/.cache/sovereign_grounding")

# module-level caches (per process)
_stdlib_cache: tuple[set[str], set[str]] | None = None       # (pkg paths, idents)
_project_cache: dict[str, tuple[float, set[str]]] = {}        # root -> (ts, tokens)
_PROJECT_CACHE_TTL = 30.0

_CAP_TOKEN = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
_SELECTOR = re.compile(r"\b([A-Za-z_]\w*)\.([A-Z]\w*)")
_DECL = re.compile(
    r"^\s*(?:func(?:\s*\([^)]*\))?\s+(\w+)"     # func Foo / func (r T) Foo
    r"|type\s+(\w+)"                            # type Foo
    r"|(?:const|var)\s+(\w+)"                   # const Foo / var Foo
    r")",
    re.MULTILINE,
)
_STRUCT_FIELD = re.compile(r"^\s*([A-Z]\w*(?:\s*,\s*[A-Z]\w*)*)\s+[\[\]\*\w\.]", re.MULTILINE)
_IMPORT_SINGLE = re.compile(r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', re.MULTILINE)
_IMPORT_BLOCK = re.compile(r"^\s*import\s*\(([^)]*)\)", re.MULTILINE | re.DOTALL)
_IMPORT_LINE = re.compile(r'^\s*(?:(\w+|\.)\s+)?"([^"]+)"', re.MULTILINE)


def _goroot() -> str | None:
    try:
        r = subprocess.run(["go", "env", "GOROOT"], capture_output=True,
                           text=True, timeout=15)
        p = r.stdout.strip()
        return p if r.returncode == 0 and p and os.path.isdir(p) else None
    except Exception:
        return None


def stdlib_whitelist() -> tuple[set[str], set[str]]:
    """Return (stdlib package import paths, stdlib exported identifiers).

    Scans GOROOT/src once and caches to ~/.cache/sovereign_grounding/ keyed by
    go version. Falls back to the static sets when Go isn't on PATH (e.g. the
    Lexar drive isn't mounted) — the gate then only hard-rejects unknown
    imports and near-miss selectors, never unknown-but-possibly-stdlib ones.
    """
    global _stdlib_cache
    if _stdlib_cache is not None:
        return _stdlib_cache

    goroot = _goroot()
    if not goroot:
        _stdlib_cache = (set(STDLIB_PACKAGES_STATIC), set())  # empty idents = degraded mode
        return _stdlib_cache

    try:
        ver = subprocess.run(["go", "version"], capture_output=True, text=True,
                             timeout=15).stdout.strip().replace(" ", "_").replace("/", "_")
    except Exception:
        ver = "unknown"
    cache_file = os.path.join(_CACHE_DIR, f"stdlib_{ver}.txt")

    if os.path.exists(cache_file):
        pkgs, idents = set(), set()
        with open(cache_file) as f:
            for line in f:
                kind, _, name = line.rstrip("\n").partition("\t")
                (pkgs if kind == "p" else idents).add(name)
        if pkgs:
            _stdlib_cache = (pkgs, idents)
            return _stdlib_cache

    src = Path(goroot) / "src"
    pkgs: set[str] = set()
    idents: set[str] = set()
    decl = re.compile(
        r"^(?:func(?:\s*\([^)]*\))?\s+([A-Z]\w*)|type\s+([A-Z]\w*)"
        r"|(?:const|var)\s+([A-Z]\w*)|\s+([A-Z]\w*(?:\s*,\s*[A-Z]\w*)*)\s+[\[\]\*\w\.])",
    )
    for p in src.rglob("*.go"):
        rel = p.relative_to(src)
        parts = rel.parts
        if ("internal" in parts or "testdata" in parts or "vendor" in parts
                or "cmd" == parts[0] or p.name.endswith("_test.go")):
            continue
        pkgs.add("/".join(parts[:-1]))
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            m = decl.match(line)
            if m:
                for g in m.groups():
                    if g:
                        for name in re.findall(r"[A-Z]\w*", g):
                            idents.add(name)
    pkgs.discard("")
    idents |= STDLIB_IDENTS_STATIC

    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(cache_file, "w") as f:
            for x in sorted(pkgs):
                f.write(f"p\t{x}\n")
            for x in sorted(idents):
                f.write(f"i\t{x}\n")
    except OSError:
        pass

    _stdlib_cache = (pkgs, idents)
    return _stdlib_cache


def project_whitelist(project_root: str) -> set[str]:
    """Every capitalized token appearing in the project's .go files.

    Deliberately permissive: the committed tree compiles, so anything in it
    is a real identifier (or a comment word — harmless surplus). If a name
    appears NOWHERE in the repo, the model cannot legitimately be calling it.
    """
    cached = _project_cache.get(project_root)
    if cached and time.time() - cached[0] < _PROJECT_CACHE_TTL:
        return cached[1]
    tokens: set[str] = set()
    skip = {".git", "build", "node_modules", "logs", ".dart_tool"}
    for p in Path(project_root).rglob("*.go"):
        if any(s in p.parts for s in skip):
            continue
        try:
            tokens |= set(_CAP_TOKEN.findall(p.read_text(errors="ignore")))
        except OSError:
            continue
    _project_cache[project_root] = (time.time(), tokens)
    return tokens


def invalidate_project_cache(project_root: str) -> None:
    _project_cache.pop(project_root, None)


def _module_info(project_root: str) -> tuple[str | None, set[str]]:
    """Return (module path from go.mod, required dependency module paths)."""
    gomod = os.path.join(project_root, "go.mod")
    if not os.path.exists(gomod):
        return None, set()
    module, requires = None, set()
    in_req = False
    for line in open(gomod):
        s = line.strip()
        if s.startswith("module "):
            module = s.split()[1]
        elif s.startswith("require ("):
            in_req = True
        elif in_req and s == ")":
            in_req = False
        elif in_req or s.startswith("require "):
            parts = s.replace("require ", "").split()
            if parts and "/" in parts[0]:
                requires.add(parts[0])
    return module, requires


def declared_names(content: str) -> set[str]:
    """Names a generated file legitimately declares itself."""
    names: set[str] = set()
    for m in _DECL.finditer(content):
        for g in m.groups():
            if g:
                names.add(g)
    # struct/interface member lines (only inside type blocks would be correct;
    # a whole-file scan is acceptable surplus)
    for m in _STRUCT_FIELD.finditer(content):
        for name in re.findall(r"[A-Z]\w*", m.group(1)):
            names.add(name)
    return names


def _imports_of(content: str) -> list[tuple[str | None, str]]:
    """[(alias_or_None, path)] for all imports in the file."""
    out: list[tuple[str | None, str]] = []
    for m in _IMPORT_BLOCK.finditer(content):
        for lm in _IMPORT_LINE.finditer(m.group(1)):
            out.append((lm.group(1), lm.group(2)))
    for m in _IMPORT_SINGLE.finditer(content):
        out.append((None, m.group(1)))
    return out


def _strip_strings_and_comments(content: str) -> str:
    content = re.sub(r"//[^\n]*", "", content)
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    content = re.sub(r'"(?:\\.|[^"\\])*"', '""', content)
    content = re.sub(r"`[^`]*`", "``", content)
    return content


def balance_go_braces(content: str, max_fix: int = 3) -> tuple[str, int]:
    """Append missing closing braces at EOF (models lose count around nesting
    depth 5-6 and emit one-too-few closers — observed identically on gemma4
    and qwen3-coder, so it's the task shape, not the model).

    Only appends when 1..max_fix braces are missing; anything else is returned
    untouched. Zero-risk: if the appended braces are wrong, gofmt/vet fails
    exactly as it would have anyway. Returns (content, braces_added).
    """
    body = _strip_strings_and_comments(content)
    diff = body.count("{") - body.count("}")
    if 0 < diff <= max_fix:
        return content.rstrip("\n") + "\n" + "}\n" * diff, diff
    return content, 0


def check_go_grounding(rel_path: str, content: str, project_root: str,
                       extra_declared: set[str] | None = None) -> list[str]:
    """Return violation messages (empty list = grounded). Never raises."""
    try:
        return _check(rel_path, content, project_root, extra_declared or set())
    except Exception as e:  # the gate must never kill a run
        print(f"  (grounding gate error, skipping: {e})")
        return []


def _check(rel_path: str, content: str, project_root: str,
           extra_declared: set[str]) -> list[str]:
    violations: list[str] = []
    stdlib_pkgs, stdlib_idents = stdlib_whitelist()
    degraded = not stdlib_idents  # no GOROOT scan → don't hard-reject unknowns
    module, requires = _module_info(project_root)

    imports = _imports_of(content)
    pkg_bases: set[str] = set()
    for alias, path in imports:
        base = alias if alias and alias != "." else path.rsplit("/", 1)[-1]
        pkg_bases.add(base)
        ok = (
            path in stdlib_pkgs
            or (degraded and "." not in path.split("/")[0])  # looks stdlib-ish
            or any(path == r or path.startswith(r + "/") for r in requires)
        )
        if not ok and module and (path == module or path.startswith(module + "/")):
            sub = path[len(module):].lstrip("/")
            ok = (sub == "") or os.path.isdir(os.path.join(project_root, sub))
        if not ok:
            violations.append(
                f"UNKNOWN IMPORT [{rel_path}]: \"{path}\" does not exist — it is not "
                f"a standard-library package, not a go.mod dependency, and not a "
                f"directory in this module{f' ({module})' if module else ''}. "
                f"Do NOT invent packages. Use only types and helpers already "
                f"defined in this package, or stdlib packages like math/sort/fmt."
            )

    proj = project_whitelist(project_root)
    declared = declared_names(content) | extra_declared
    body = _strip_strings_and_comments(content)

    unknown: dict[str, str] = {}  # ident -> example expression
    for m in _SELECTOR.finditer(body):
        base, sel = m.group(1), m.group(2)
        if base in pkg_bases:
            continue  # pkg.Func — validity of pkg checked via its import path
        if sel in proj or sel in declared or sel in stdlib_idents:
            continue
        if degraded:
            # without a stdlib scan only reject near-misses of project names
            close = difflib.get_close_matches(sel, proj, n=1, cutoff=0.75)
            if not close and sel.lower() not in {p.lower() for p in proj}:
                continue
        unknown.setdefault(sel, f"{base}.{sel}")

    # Bare exported identifiers (e.g. `g.SpawnSquad(Opponent, ...)` where
    # Opponent isn't a constant, or invented types like Registry / Vec) —
    # the selector regex above only sees x.Foo, so check standalone
    # capitalized tokens too.
    for tok in set(_CAP_TOKEN.findall(body)):
        if (tok in proj or tok in declared or tok in stdlib_idents
                or tok in pkg_bases or tok in unknown):
            continue
        if degraded:
            close = difflib.get_close_matches(tok, proj, n=1, cutoff=0.75)
            if not close and tok.lower() not in {p.lower() for p in proj}:
                continue
        unknown.setdefault(tok, tok)

    for sel, expr in sorted(unknown.items()):
        close = difflib.get_close_matches(sel, sorted(proj), n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        violations.append(
            f"UNKNOWN IDENTIFIER [{rel_path}]: `{expr}` — `{sel}` is not defined "
            f"anywhere in this project or the Go standard library. Do NOT invent "
            f"fields, methods, helpers, types, or constants.{hint} If you need "
            f"data, access the existing exported fields/methods of the locked "
            f"types directly."
        )
    return violations
