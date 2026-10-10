"""Check a kit against a reference solution before any worker runs.

WHY (2026-10-03, xmeasure v2)

Three merged files that night had bugs their own gates could not see:
parse_role_row read an optional column unconditionally, reopen_rate passed a
keyword make_result does not take on its empty branch, data_gaps dropped v1
'no source' gaps. Each gate tested only the happy path, so each bug merged
green and surfaced hours later as a DIFFERENT task failing — and three tasks
were blamed for a fault that was not theirs. A gate that only fails on
MISSING code is not a gate.

WHAT

simulate(project, reference) works on a throwaway copy of the project at HEAD
and walks the open tasks in ROADMAP order:

  1. its gate must FAIL before the task's code exists ("gate passes without
     the code" otherwise);
  2. the task's own functions are applied from the reference (Python: only
     the functions the task names, plus the imports, constants and private
     helpers they use, so a later task on the same file is still checked
     honestly; anything else: the whole file);
  3. its gate must now PASS ("gate fails on the reference" otherwise — a kit
     inconsistency or an ordering problem);
  4. mutants: small plausible mistakes in each of the task's functions — a
     flipped comparison, a condition forced True/False, an if-body emptied,
     a return of None, a constant nudged, and/or swapped. Every mutant the
     gate still passes is reported: that is a mistake a worker can merge.

Surviving mutants are leads, not verdicts (some are equivalent), but a task
with several is a gate worth strengthening before a model sees it.
"""
import ast
import copy
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time


def _own_names(task: str) -> list[str]:
    head = task.split("— done when:")[0].split(" — ", 1)[0]
    return list(dict.fromkeys(re.findall(r"`(?:[\w\[\], |.]+\s)?(\w+)\s*\(", head)))


def _target(task: str) -> str | None:
    m = re.search(r"\bIn `?([\w./-]+\.\w+)`?:", task)
    return m.group(1) if m else None


def _gate(task: str) -> str | None:
    m = re.search(r"task gate:\s*(.+?)\s+passes\s*$", task.strip())
    return m.group(1) if m else None


# A shared venv with an editable install (`pip install -e`) resolves any module
# the copy lacks to the LIVE project — gates in the copy passed on code that
# only existed there (2026-10-03). This drops those finders at startup.
_SITECUSTOMIZE = """import sys
sys.meta_path[:] = [f for f in sys.meta_path if 'editable' not in type(f).__name__.lower()
                    and 'editable' not in getattr(f, '__name__', '').lower()]
"""


def isolated_env(work: str) -> dict:
    site = os.path.join(work, ".refcheck_site")
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "sitecustomize.py"), "w") as f:
        f.write(_SITECUSTOMIZE)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")   # a mutant's .pyc must never outlive it
    env["PYTHONPATH"] = site + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def run_gate(gate: str, cwd: str, timeout: int = 180, out: list | None = None, env: dict | None = None) -> bool:
    """True when the gate passes. On timeout the whole process group is killed —
    killing only `bash` left a looping pytest running at full CPU. Pass `out`
    (a list) to receive the tail of the gate's output."""
    p = subprocess.Popen(["bash", "-c", gate], cwd=cwd, stdout=subprocess.PIPE, env=env,
                         stderr=subprocess.STDOUT, start_new_session=True, text=True)
    try:
        text, _ = p.communicate(timeout=timeout)
        if out is not None:
            out.append(_evidence(text or ""))
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        if out is not None:
            out.append(f"timed out after {timeout}s")
        return False


def _evidence(text: str) -> str:
    errs = [l.strip() for l in text.splitlines() if l.startswith("E ") or "Error" in l]
    return (errs[0] if errs else text.strip().splitlines()[-1] if text.strip() else "")[:200]


# ─── applying a task's reference code ────────────────────────────────────────

def _top(tree: ast.Module) -> dict[str, ast.AST]:
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = n
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out[t.id] = n
    return out


def _used_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def apply_reference(task: str, cur_src: str | None, ref_src: str) -> str:
    """cur_src with the task's own functions (and what they need) taken from ref_src."""
    ref = ast.parse(ref_src)
    cur = ast.parse(cur_src or "")
    ref_top, cur_top = _top(ref), _top(cur)
    own = [n for n in _own_names(task) if n in ref_top]
    if not own:                                           # nothing named: take the whole file
        return ref_src
    want, frontier = set(own), list(own)
    while frontier:                                       # private helpers and constants they use
        for name in _used_names(ref_top[frontier.pop()]):
            # private helpers and module constants come along; another task's
            # public function does not — needing one that hasn't landed is an
            # ordering problem the gate should show.
            # (constants are always refreshed: an "extend" task may change one)
            constant = isinstance(ref_top.get(name), (ast.Assign, ast.AnnAssign))
            if name in ref_top and name not in want and (name.startswith("_") or constant):
                want.add(name)
                frontier.append(name)
    body = list(cur.body)
    have_imports = {ast.unparse(n) for n in body if isinstance(n, (ast.Import, ast.ImportFrom))}
    imports = [n for n in ref.body if isinstance(n, (ast.Import, ast.ImportFrom))
               and ast.unparse(n) not in have_imports]
    for name in [n for n in _top(ref) if n in want]:      # reference order
        node = ref_top[name]
        idx = next((i for i, b in enumerate(body) if name in _top(ast.Module(body=[b], type_ignores=[]))), None)
        if idx is None:
            body.append(node)
        else:
            body[idx] = node
    last_import = max((i for i, b in enumerate(body) if isinstance(b, (ast.Import, ast.ImportFrom))), default=-1)
    body[last_import + 1:last_import + 1] = imports
    return ast.unparse(ast.Module(body=body, type_ignores=[])) + "\n"


# ─── mutants ─────────────────────────────────────────────────────────────────

_FLIP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
         ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}


def mutants(func: ast.AST, limit: int = 14) -> list[tuple[str, ast.AST]]:
    """Deterministic single-point mutations of one function: (description, mutated copy)."""
    sites = []
    for node in ast.walk(func):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _FLIP:
                    sites.append((f"line {line}: {type(op).__name__} → {_FLIP[type(op)].__name__}", node, ("cmp", i)))
        elif isinstance(node, ast.If):
            sites.append((f"line {line}: if-condition forced True", node, ("if", True)))
            sites.append((f"line {line}: if-condition forced False", node, ("if", False)))
        elif isinstance(node, ast.IfExp):
            sites.append((f"line {line}: conditional expression forced True", node, ("ifexp", True)))
            sites.append((f"line {line}: conditional expression forced False", node, ("ifexp", False)))
        elif isinstance(node, ast.BoolOp):
            sites.append((f"line {line}: {type(node.op).__name__} → {'Or' if isinstance(node.op, ast.And) else 'And'}",
                          node, ("bool", None)))
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            sites.append((f"line {line}: returns None", node, ("ret", None)))
        elif isinstance(node, ast.Constant) and type(node.value) in (int, float) and not isinstance(node.value, bool):
            sites.append((f"line {line}: constant {node.value!r} → {node.value + 1!r}", node, ("const", None)))
    if len(sites) > limit:                                # spread across the function, not the first N
        step = len(sites) / limit
        sites = [sites[int(i * step)] for i in range(limit)]
    out = []
    for desc, target, (kind, arg) in sites:
        f2 = copy.deepcopy(func)
        # locate the twin of `target` in the copy by walk position
        pos = next(i for i, n in enumerate(ast.walk(func)) if n is target)
        twin = next(n for i, n in enumerate(ast.walk(f2)) if i == pos)
        if kind == "cmp":
            twin.ops[arg] = _FLIP[type(twin.ops[arg])]()
        elif kind == "if":
            twin.test = ast.Constant(arg)
        elif kind == "ifexp":
            twin.test = ast.Constant(arg)
        elif kind == "bool":
            twin.op = ast.Or() if isinstance(twin.op, ast.And) else ast.And()
        elif kind == "ret":
            twin.value = ast.Constant(None)
        elif kind == "const":
            twin.value = twin.value + 1
        out.append((desc, ast.fix_missing_locations(f2)))
    return out



# ─── Dart mutants (text level: no Dart parser here) ─────────────────────────
# The AST mutants above are Python-only, so a Dart task reported "0 weak gates"
# having run none (magic_task_hat 2026-10-10). These are single-point textual
# mutations on code outside strings, comments and directives, plus the two
# string kinds a Flutter gate depends on: message keys t('a.b') and Key('x').

def _dart_code_mask(src: str) -> list[bool]:
    """True where a character is code (not a string, comment, or import/export/part line)."""
    mask = [True] * len(src)
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i); j = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2); j = n if j < 0 else j + 2
        elif src[i] in "'\"":
            q = src[i] * 3 if src.startswith(src[i] * 3, i) else src[i]
            j = i + len(q)
            while j < n and not src.startswith(q, j):
                j += 2 if src[j] == "\\" else 1
            j = min(n, j + len(q))
        elif (i == 0 or src[i - 1] == "\n") and re.match(r"(import|export|part|library)\b", src[i:i + 8]):
            j = src.find(";", i); j = n if j < 0 else j + 1
        else:
            i += 1
            continue
        for k in range(i, j):
            mask[k] = False
        i = j
    return mask


_DART_OPS = [
    (re.compile(r" == "), " != "), (re.compile(r" != "), " == "),
    (re.compile(r" < "), " <= "), (re.compile(r" <= "), " < "), (re.compile(r" > "), " >= "), (re.compile(r" >= "), " > "),
    (re.compile(r" && "), " || "), (re.compile(r" \|\| "), " && "),
    (re.compile(r"\.isEmpty\b"), ".isNotEmpty"), (re.compile(r"\.isNotEmpty\b"), ".isEmpty"),
    (re.compile(r"\btrue\b"), "false"), (re.compile(r"\bfalse\b"), "true"),
    (re.compile(r"!(?=[\w(])"), ""),
]
_DART_STRINGS = [re.compile(r"\bt\('([a-zA-Z]\w*(?:\.\w+)+)'"), re.compile(r"\bKey\('([^'\\]+)'\)")]


def dart_mutants(src: str, limit: int = 20) -> list[tuple[str, str]]:
    """Deterministic single-point textual mutations of a Dart file: (description, mutated source)."""
    mask = _dart_code_mask(src)
    line = lambda pos: src.count("\n", 0, pos) + 1
    sites = []
    for rx, rep in _DART_OPS:
        for m in rx.finditer(src):
            if all(mask[m.start():m.end()]):
                sites.append((m.start(), f"line {line(m.start())}: {m.group(0).strip() or m.group(0)} → {rep.strip() or 'removed'}",
                              src[:m.start()] + rep + src[m.end():]))
    for rx in _DART_STRINGS:
        for m in rx.finditer(src):
            if mask[m.start()]:
                a, b = m.span(1)
                sites.append((a, f"line {line(a)}: '{m.group(1)}' → '{m.group(1)}X'", src[:b] + "X" + src[b:]))
    sites.sort(key=lambda s: s[0])
    if len(sites) > limit:
        step = len(sites) / limit
        sites = [sites[int(i * step)] for i in range(limit)]
    return [(d, s) for _, d, s in sites]


def _with_function(src: str, name: str, func: ast.AST) -> str:
    tree = ast.parse(src)
    tree.body = [func if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name else n
                 for n in tree.body]
    return ast.unparse(tree) + "\n"


# ─── the walk ────────────────────────────────────────────────────────────────

def _copy_project(root: str) -> str:
    dst = tempfile.mkdtemp(prefix="xanadu-refcheck-")
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", root, dst], check=True, capture_output=True)
    for venv in (".venv", "venv"):
        if os.path.isdir(os.path.join(root, venv)):
            os.symlink(os.path.join(root, venv), os.path.join(dst, venv))
    return dst


def simulate(root: str, ref_root: str, mutate: bool = True, max_mutants: int = 14,
             only: str | None = None, log=print) -> list[dict]:
    """One result per open task: {target, fails_before, passes_after, survivors, mutants, note}."""
    roadmap = open(os.path.join(root, "ROADMAP.md")).read()
    tasks = [l[6:].strip() for l in roadmap.splitlines() if l.startswith("- [ ]")]
    work = _copy_project(root)
    env = isolated_env(work)
    results = []
    try:
        for i, task in enumerate(tasks, 1):
            target, gate = _target(task), _gate(task)
            res = {"target": target, "task": task[:100], "fails_before": None, "passes_after": None,
                   "survivors": [], "mutants": 0, "note": ""}
            results.append(res)
            ref_path = os.path.join(ref_root, target or "")
            if not target or not gate or not os.path.isfile(ref_path):
                res["note"] = "no gate" if not gate else "no reference file"
                log(f"[{i}/{len(tasks)}] {target}: skipped ({res['note']})")
                continue
            path = os.path.join(work, target)
            res["fails_before"] = not run_gate(gate, work, env=env)
            cur = open(path).read() if os.path.exists(path) else None
            ref = open(ref_path).read()
            try:
                new = apply_reference(task, cur, ref) if target.endswith(".py") else ref
            except SyntaxError as exc:
                res["note"] = f"could not apply reference: {exc}"
                new = ref
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            open(path, "w").write(new)
            t0 = time.time()
            ev: list = []
            res["passes_after"] = run_gate(gate, work, out=ev, env=env)
            if not res["passes_after"]:
                res["note"] = ev[0] if ev else ""
            # A mutant can loop forever (a pagination `while` forced True): give
            # it a few times the real gate's time; timing out counts as caught.
            mutant_timeout = max(10, int((time.time() - t0) * 5))
            if mutate and res["passes_after"] and target.endswith(".py") and (only is None or only in target):
                tree = ast.parse(new)
                funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                for name in [n for n in _own_names(task) if n in funcs]:
                    for desc, mut in mutants(funcs[name], max_mutants):
                        open(path, "w").write(_with_function(new, name, mut))
                        res["mutants"] += 1
                        if run_gate(gate, work, timeout=mutant_timeout, env=env):
                            res["survivors"].append(f"{name} {desc}")
                    open(path, "w").write(new)
            if mutate and res["passes_after"] and target.endswith(".dart") and (only is None or only in target):
                for desc, mut in dart_mutants(new, max_mutants):
                    open(path, "w").write(mut)
                    res["mutants"] += 1
                    if run_gate(gate, work, timeout=max(mutant_timeout, 60), env=env):
                        res["survivors"].append(desc)
                open(path, "w").write(new)
            flag = ("" if res["fails_before"] else " GATE PASSES WITHOUT THE CODE") + \
                   ("" if res["passes_after"] else " GATE FAILS ON THE REFERENCE") + \
                   (f" {len(res['survivors'])}/{res['mutants']} mutants survive" if res["survivors"] else "")
            log(f"[{i}/{len(tasks)}] {target}:{flag or ' ok'}" + (f"  ({res['note']})" if res["note"] else ""))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return results
