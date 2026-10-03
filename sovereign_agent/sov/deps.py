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
    m = re.search(r'\bIn `?([\w./-]+\.tsx?)`?:', task)
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
    tm = re.search(r"\bIn `?([\w./-]+\.dart)`?:", task)
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


def _py_defined_names(project_root: str) -> set[str]:
    """Top-level functions, classes and assignments across the project's source."""
    import ast
    names: set[str] = set()
    for dirpath, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("tests", "test", "logs", "node_modules", "__pycache__", "venv")]
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(dirpath, f), errors="ignore").read())
            except (SyntaxError, OSError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, ast.Assign):
                    names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def gate_test_sources(gate_cmd: str, project_root: str) -> list[str]:
    """Source of the tests a gate actually runs. A node `file.py::test_x` keeps the
    module-level code (imports, helpers) and ONLY the named test functions — every
    other `test_*` is stripped. Scanning whole files attributed sibling tests'
    imports to the task: jira_issue_v2 then "depended on" jira_history, which
    depends on it — a deferral deadlock (2026-10-01)."""
    import ast
    picked: dict[str, set[str] | None] = {}
    for node in re.findall(r"([\w./-]+\.py(?:::[\w\[\]-]+)?)", gate_cmd):
        path, _, test = node.partition("::")
        if test:
            if picked.get(path, set()) is not None:
                picked.setdefault(path, set()).add(test)
        else:
            picked[path] = None                       # whole file
    out = []
    for path, tests in picked.items():
        try:
            src = open(os.path.join(project_root, path), errors="ignore").read()
        except OSError:
            continue
        if tests is None:
            out.append(src)
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            out.append(src)
            continue
        keep = [n for n in tree.body
                if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
                        and n.name not in tests)]
        out.append("\n".join(ast.get_source_segment(src, n) or "" for n in keep))
    return out


def _missing_python_deps(task: str, project_root: str) -> list[str]:
    """What a Python task needs that hasn't landed: project modules it names
    (`pkg.mod` / `from pkg.mod import x`) that don't exist, and functions it
    tells the worker to call that nothing defines yet. 2026-09-30: strong-pass
    ordering put compute.py before compute_quality.py, which it imports; with
    no Python check every attempt was certain to fail."""
    import builtins
    import keyword
    # Any target — a template's gate runs Python tests too: report.html.j2's
    # gate needed compute.py (v2) and its partials, and ran 3 times before
    # they existed because non-.py targets skipped this check (2026-10-02).
    tm = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", task)
    if not tm:
        return []
    target = tm.group(1)
    pkg = target.split("/")[0]
    body = task.split("— done when:")[0]
    missing: list[str] = []
    for dotted in sorted(set(re.findall(rf"\b{re.escape(pkg)}(?:\.\w+)+", body))):
        parts = dotted.split(".")
        if parts[-1] in _FILE_EXTS:
            continue                                 # 'xmeasure.yaml' is a file name
        for i in range(len(parts), 0, -1):          # longest prefix that exists
            base = os.path.join(project_root, *parts[:i])
            if os.path.isfile(base + ".py"):
                break                                # the rest are attributes
            if os.path.isdir(base):
                if i < len(parts):
                    mod = "/".join(parts[:i + 1]) + ".py"
                    if mod != target:
                        missing.append(mod)
                break
    defined = _py_defined_names(project_root)
    # The functions THIS task defines: every signature before the first " — "
    # ("implement the pair `save_x(...)` / `load_x(...)`", "extend `f(...)`").
    head = body.split(" — ", 1)[0]
    sigs = re.findall(r"`(?:[\w\[\], |.]+\s)?(\w+)\s*\(([^`]*)\)", head)
    own_names = {n for n, _ in sigs}
    own_name = sigs[0][0] if sigs else None
    # Its own parameters are callable too: `log(...)` in collect(…, log=print)
    # deferred collect forever (2026-09-30).
    params = {p for _, args in sigs for p in re.findall(r"(?:^|,)\s*(\w+)\s*[:=]", args)}
    # Names the task imports from OUTSIDE the project are available (`from
    # dataclasses import replace`). Names it imports from a project module must
    # really exist there: `from xmeasure.metrics.issue_history import
    # was_reopened` hid that was_reopened's task hadn't landed, and reopen.py
    # failed three times on the ImportError (2026-10-02).
    imported: set[str] = set()
    for mod, grp in re.findall(r"\bfrom\s+([\w.]+)\s+import\s+([\w, ]+)", body):
        names = [n.strip().split(" as ")[-1] for n in grp.split(",") if n.strip()]
        if mod.split(".")[0] != pkg:
            imported |= set(names)
            continue
        mpath = os.path.join(project_root, *mod.split(".")) + ".py"
        if mpath.endswith(target) or not os.path.exists(mpath):
            continue                                  # a missing module is reported above
        have = _module_names(mpath)
        missing += [f"{n}()" for n in names if n not in have and n[:1].islower()]
    imported |= {n.strip().split(" as ")[-1] for grp in re.findall(r"(?<![\w.])import\s+([\w, ]+)", body)
                 if not grp.strip().startswith(pkg) for n in grp.split(",") if n.strip()}
    # Names the task itself binds are locals, not project functions: literal-code
    # tasks write `parse = PARSERS[cov.format]` then `parse(f.read_text())`, or
    # "where agg is `mean`…" then `agg(lv[m])`, and both were deferred forever
    # as missing dependencies — the sweep hit a false fixpoint (2026-10-03).
    local = set()
    for code in re.findall(r"`([^`]+)`", body):
        local |= set(re.findall(r"(?<![\w.=!<>])([a-z_]\w*)\s*=(?!=)", code))
        local |= set(re.findall(r"\bfor\s+([a-z_]\w*)\s+in\b", code))
    local |= set(re.findall(r"\bwhere\s+`?([a-z_]\w*)`?\s+is\b", body))
    defined = defined | params | imported | local
    for code in (re.findall(r"`([^`]+)`", body) if target.endswith(".py") else []):   # templates: Jinja calls
        # A call is `name(` with no space ("report ({e})" inside a format string is
        # not one); _private helpers belong to the task that writes them.
        for name in re.findall(r"(?<![\w.])([a-z]\w*)\(", code):
            if name in own_names or hasattr(builtins, name) or name in defined or name in _stdlib_names():
                continue
            if keyword.iskeyword(name) or name == "print":    # `else (` in `… if x else ()` is not a call
                continue
            missing.append(f"{name}()")
    # The gate's own test may import modules the task text never names:
    # test_report_html imports report.build, so html.py could not pass before
    # build.py landed (two 15-min attempts on an unfixable error, 2026-09-30).
    gate = re.search(r"task gate:\s*(.+?)\s+passes", task)
    if gate:
        for tsrc in gate_test_sources(gate.group(1), project_root):
            for mod in set(re.findall(rf"^\s*(?:from|import)\s+({re.escape(pkg)}(?:\.\w+)+)", tsrc, re.M)):
                path = mod.replace(".", "/") + ".py"
                if path != target and not os.path.exists(os.path.join(project_root, path)) \
                        and not os.path.isdir(os.path.join(project_root, mod.replace(".", "/"))):
                    missing.append(path)

    # Non-Python files another task builds (templates, data): html.py's task
    # names report.html.j2, and with the template task failed, html.py burned
    # 30 minutes on a file that did not exist (2026-10-01 dry run). Only files
    # some ROADMAP task creates count, so config names like 'xmeasure.yaml' don't.
    try:
        rm_text = open(os.path.join(project_root, "ROADMAP.md"), errors="ignore").read()
    except OSError:
        rm_text = ""
    gate_src = "\n".join(gate_test_sources(gate.group(1), project_root)) if gate else ""
    for other in set(re.findall(r"^- \[[ x]\] In `?([\w./-]+\.\w+)`?:", rm_text, re.M)):
        if other == target or other.endswith(".py"):
            continue
        name = os.path.basename(other)
        if (name in body or name in gate_src) and not os.path.exists(os.path.join(project_root, other)):
            missing.append(other)

    # An EARLIER open task on a module the gate imports (or the task names) is
    # unfinished work this task needs even though the module exists: the
    # scorecard partials render through build_report_data, whose v2 task in
    # report/build.py was still open (2026-10-02). Earlier tasks only — no cycles.
    gate_text = "\n".join(gate_test_sources(gate.group(1), project_root)) if gate else ""
    gate_mods = {m.replace(".", "/") + ".py"
                 for m in re.findall(rf"^\s*(?:from|import)\s+({re.escape(pkg)}(?:\.\w+)+)", gate_text, re.M)}
    gate_mods |= {m.replace(".", "/") + ".py" for m in re.findall(rf"\b{re.escape(pkg)}(?:\.\w+)+", body)}
    me = task.strip()[:100]
    for line in rm_text.splitlines():
        if line[6:].strip()[:100] == me:
            break                                     # only tasks BEFORE this one
        om = re.match(r"^- \[ \] In `?([\w./-]+\.\w+)`?:", line)
        if not om or om.group(1) not in gate_mods or om.group(1) == target:
            continue
        # ...and only when this task or its gate USES a function that task builds
        # (estimate_consistency needs history_by_key, not the still-open was_reopened)
        built = {n for n, _ in re.findall(r"`(?:[\w\[\], |.]+\s)?(\w+)\s*\(([^`]*)\)", line[6:].split(" — ", 1)[0])}
        if not built or any(re.search(rf"\b{re.escape(n)}\b", gate_text + "\n" + body) for n in built):
            missing.append(om.group(1))

    # API.md-style cheat sheets map each function to its file (`# pkg/mod.py`
    # then `def name(`). A function the task names whose file hasn't landed
    # is missing even when the task doesn't backtick it.
    for name, path in _api_md_functions(project_root).items():
        if path != target and not os.path.exists(os.path.join(project_root, path)) \
                and re.search(rf"(?<![\w.-]){re.escape(name)}\((?!-)", body) and name != own_name:
            missing.append(path)
    return sorted(set(missing))


_FILE_EXTS = {"yaml", "yml", "json", "db", "csv", "html", "md", "txt", "toml", "j2", "pdf", "log", "jsonl"}


def _api_md_functions(project_root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "logs", "tests")]
        if "API.md" not in files:
            continue
        current = None
        for line in open(os.path.join(dirpath, "API.md"), errors="ignore"):
            h = re.match(r"#\s+([\w./-]+\.py)\b", line.strip())
            if h:
                current = h.group(1)
                continue
            d = re.match(r"(?:async\s+)?def\s+(\w+)\s*\(", line.strip())
            if d and current:
                out.setdefault(d.group(1), current)
    return out


_STDLIB_CACHE: set[str] | None = None


def _stdlib_names() -> set[str]:
    """Callables exported by the standard-library modules tasks mention most.
    `timedelta(days=…)` in a task text is not a missing project function."""
    global _STDLIB_CACHE
    if _STDLIB_CACHE is None:
        import importlib
        names: set[str] = set()
        for mod in ("datetime", "pathlib", "collections", "itertools", "functools", "math",
                    "statistics", "re", "json", "os", "os.path", "csv", "dataclasses", "typing",
                    "urllib.parse", "urllib.request", "time", "sqlite3", "subprocess", "argparse",
                    "fnmatch", "hashlib", "base64", "enum", "shutil", "tempfile", "random", "uuid"):
            try:
                names |= {n for n in dir(importlib.import_module(mod)) if not n.startswith("_")}
            except ImportError:
                pass
        _STDLIB_CACHE = names
    return _STDLIB_CACHE


def _module_names(path: str) -> set[str]:
    """Top-level names a module defines or imports."""
    import ast
    try:
        tree = ast.parse(open(path, errors="ignore").read())
    except (SyntaxError, OSError):
        return set()
    out: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {a.asname or a.name.split(".")[0] for a in n.names}
    return out
