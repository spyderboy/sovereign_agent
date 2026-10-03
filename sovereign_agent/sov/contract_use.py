"""Did the worker use the contract it was given, or copy it?

A task that says "pair results ... with compare_metric" can pass its gate by
pasting compare_metric's logic into the new function instead of calling it.
The gate is green, the duplicate drifts, and a bug in the real function is
hidden: on 2026-10-01 a preflight dry run "passed" compare_all exactly that
way, over a compare.py that still stored the wrong types. The new-project
skill's "check that the worker CONSUMED the contract" step, enforced.

Python projects only. A function counts as one the task tells the worker to
use when the task text names it, it already exists elsewhere in the project,
and the mention isn't negated ("never call X", "instead of X").
"""
import ast
import os
import re

# A comparison is not an instruction either: "like make_result but with
# min_n" and "secrets as in collect_jira" point at a model to imitate, and
# demanding a call there blocked two correct tasks three times each (2026-10-02).
_NEGATION = re.compile(r"\b(?:never|not|don'?t|do not|without|instead of|avoid|no|nor|rather than|keep|"
                       r"like|as in|same as|similar to|mirrors?|mirroring|based on)\b[^.;]{0,40}$",
                       re.I)


def _functions(project_root: str, skip: set[str]) -> dict[str, str]:
    """Top-level functions in the project's own source → defining file."""
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("tests", "test", "logs", "node_modules", "__pycache__", "venv", "spec")]
        for f in files:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), project_root)
            if rel in skip:
                continue
            try:
                tree = ast.parse(open(os.path.join(dirpath, f), errors="ignore").read())
            except (SyntaxError, OSError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                    out.setdefault(node.name, rel)
    return out


def named_functions(task: str, project_root: str) -> dict[str, str]:
    """Existing project functions the task tells the worker to use → their file."""
    tm = re.search(r"\bIn `?([\w./-]+\.py)`?:", task)
    if not tm:
        return {}
    target = tm.group(1)
    body = task.split("— done when:")[0]
    own = re.search(r"implement `(?:[\w\[\], |.]+\s)?(\w+)\s*\(", body)
    own_name = own.group(1) if own else None
    found: dict[str, str] = {}
    for name, path in _functions(project_root, {target}).items():
        if name == own_name:
            continue
        for m in re.finditer(rf"(?<![\w.]){re.escape(name)}\b", body):
            if not _NEGATION.search(body[max(0, m.start() - 60):m.start()]):
                found[name] = path
                break
    return found


def unused_contract(task: str, written: list[str], project_root: str) -> str | None:
    """An error for the retry when the written code never references a function
    the task told it to use; None when it does (or nothing applies)."""
    wanted = named_functions(task, project_root)
    if not wanted:
        return None
    code = ""
    for rel in written:
        if rel.endswith(".py"):
            try:
                code += open(os.path.join(project_root, rel), errors="ignore").read()
            except OSError:
                pass
    if not code:
        return None
    missing = [n for n in wanted if not re.search(rf"(?<![\w.]){re.escape(n)}\b", code)]
    if not missing:
        return None
    lines = ", ".join(f"`{n}` (in {wanted[n]})" for n in missing)
    return (f"CONTRACT NOT USED: the task tells you to use {lines}, but your code never calls "
            f"{'it' if len(missing) == 1 else 'them'}. Import and call the existing function — do "
            f"not copy or re-implement its logic; a copy drifts and hides bugs in the original.")
