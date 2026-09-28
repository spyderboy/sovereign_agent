"""Dependency checks: the project symbol index and detection of task imports
that do not exist yet (so a task is deferred rather than failed).

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
import import_fixer
import os
import re


# Cache of (symbol->module, module->exports) per project_root. Cheap to build
# (regex over the source tree) but rebuilt only when explicitly invalidated;
# exports change rarely between tasks and a slightly stale map is still correct
# for the vast majority of symbols.
_SYMBOL_INDEX_CACHE: dict[str, tuple[dict, dict, dict]] = {}


def _symbol_index(project_root: str, refresh: bool = False):
    """Memoized (symbol->module, module->exports, funcname->signature)."""
    key = os.path.abspath(project_root)
    if refresh or key not in _SYMBOL_INDEX_CACHE:
        try:
            idx, exp = import_fixer.build_symbol_index(project_root)
            sigs = import_fixer.build_signature_index(project_root)
            _SYMBOL_INDEX_CACHE[key] = (idx, exp, sigs)
        except Exception:
            _SYMBOL_INDEX_CACHE[key] = ({}, {}, {})
    return _SYMBOL_INDEX_CACHE[key]


def _target_ts_file(task: str) -> str | None:
    """Extract the target .ts/.tsx path a task names: 'In src/sim/foo.ts: ...'."""
    m = re.search(r'\bIn ([\w./-]+\.tsx?):', task)
    return m.group(1) if m else None


def _module_exists(base_no_ext: str, project_root: str) -> bool:
    """True if a project module resolves to a real file (.ts/.tsx/.json/index)."""
    for cand in (base_no_ext + ".ts", base_no_ext + ".tsx", base_no_ext + ".json",
                 os.path.join(base_no_ext, "index.ts"),
                 os.path.join(base_no_ext, "index.tsx")):
        if os.path.exists(os.path.join(project_root, cand)):
            return True
    return False



def _missing_dart_deps(task: str, project_root: str) -> list[str]:
    """Dart files this task depends on that do not exist yet.

    Two sources, both explicit — nothing is inferred:

      1. the task text's own "Import x from ../board.dart" clauses, resolved
         against the target file's directory;
      2. the GATE's imports. A task gated on a conformance test that imports a
         module a LATER task builds cannot pass however good the worker is.

    Deferring costs nothing: the task stays unchecked and the next pass picks
    it up once its dependency has landed. Burning its budget costs a full tier
    ladder and, worse, produces a green-looking merge full of stubs invented to
    satisfy the missing import.
    """
    tm = re.search(r"\bIn ([\w./-]+\.dart):", task)
    if not tm:
        return []
    target = tm.group(1)
    target_dir = os.path.dirname(target)
    missing: list[str] = []

    def _check(path: str) -> None:
        path = os.path.normpath(path)
        if path == target or path in missing:
            return
        if not path.startswith(("lib/", "test/")):
            return
        if not os.path.exists(os.path.join(project_root, path)):
            missing.append(path)

    for rel in re.findall(r"from ([\w./-]+\.dart)", task):
        _check(rel if "/" in rel and not rel.startswith(".")
               else os.path.join(target_dir, rel))

    gm = re.search(r"task gate:.*?(test/[\w./-]+\.dart)", task)
    if gm:
        gate = os.path.join(project_root, gm.group(1))
        if os.path.exists(gate):
            pkg = ""
            _pub = os.path.join(project_root, "pubspec.yaml")
            if os.path.exists(_pub):
                _nm = re.search(r"^name:\s*(\S+)", open(_pub).read(), re.M)
                pkg = _nm.group(1) if _nm else ""
            src = open(gate).read()
            for uri in re.findall(r"^import '([^']+)';", src, re.M):
                if pkg and uri.startswith(f"package:{pkg}/"):
                    _check("lib/" + uri.split(f"package:{pkg}/", 1)[1])
    return missing


def _missing_local_deps(task: str, project_root: str) -> list[str]:
    """Project source files a task says it depends on that do NOT exist yet.

    A composition/integration task that imports a sibling module which hasn't
    landed can never pass — the worker just hallucinates the missing symbol.
    Rather than burn every attempt, the caller defers such a task to the retry
    pass (returns "skipped") so its dependency has a chance to land first.

    Only counts EXPLICIT project paths written in the task text ("src/render/
    useGameLoop", "src/sim/lasso.ts") and quoted relative imports ("../sim/x").
    Never flags the task's own target file, node_modules, or framework pkgs, so
    a task with no named-but-missing dependency is never falsely deferred.
    """
    target = _target_ts_file(task)
    target_dir = os.path.dirname(target) if target else ""
    target_no_ext = re.sub(r'\.tsx?$', '', target) if target else None

    candidates: set[str] = set()

    # Absolute-style project references: src/render/useGameLoop(.ts)?
    for m in re.findall(r'\bsrc/[\w./-]+', task):
        p = m.rstrip('.,;)')
        if p.endswith((".md",)):        # spec docs handled elsewhere
            continue
        candidates.add(re.sub(r'\.tsx?$', '', p))

    # Quoted relative imports resolved against the target's directory.
    if target_dir:
        for rel in re.findall(r"""['"](\.\.?/[\w./-]+)['"]""", task):
            resolved = os.path.normpath(os.path.join(target_dir, rel))
            candidates.add(re.sub(r'\.tsx?$', '', resolved))

    missing = []
    for c in sorted(candidates):
        if target_no_ext and c == target_no_ext:
            continue                    # never flag the file we're writing
        if not c.startswith("src/"):
            continue                    # only project sources
        if os.path.isdir(os.path.join(project_root, c)):
            continue                    # a directory mention, not a module import
        if not _module_exists(c, project_root):
            missing.append(c)
    return missing
