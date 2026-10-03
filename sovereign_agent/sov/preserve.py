"""Put back the functions a task did not ask to change.

A task that adds one function to an existing file says "keep the others
exactly as they are" — and a local model regenerates the whole file from
memory anyway. On 2026-10-03 xanadu_unlinked_tasks rewrote xanadu_story_share
(2 parameters, then 0.5 instead of 50.0) across ten attempts, and story_split
appended its own broken copy of story_values below the real one. Task text
cannot fix that; the model is not choosing to change them.

So after every write: for a Python target that existed before the task, each
PUBLIC top-level function or class the task does not name as its own (the
signatures before the first " — ") is restored to its original source, and any
import the original file had but the new one dropped is re-added. Private
`_helpers` are left alone — a task may legitimately reshape those.
"""
import ast
import re
import subprocess


def _own_names(task: str) -> set[str]:
    body = task.split("— done when:")[0]
    head = body.split(" — ", 1)[0]
    return {n for n in re.findall(r"`(?:[\w\[\], |.]+\s)?(\w+)\s*\(", head)}


def _defs(tree: ast.Module) -> list:
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _span(node) -> tuple[int, int]:
    """0-based [start, end) line range, decorators included."""
    start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno]) - 1
    return start, node.end_lineno


def original_source(rel: str, project_root: str) -> str | None:
    r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=project_root, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def restore(task: str, new_src: str, orig_src: str) -> tuple[str, list[str]]:
    """new_src with every untouched public def put back as it was in orig_src.
    Returns (source, restored names). Unparseable input is returned unchanged."""
    try:
        orig_tree, new_tree = ast.parse(orig_src), ast.parse(new_src)
    except SyntaxError:
        return new_src, []
    own = _own_names(task)
    orig_lines = orig_src.splitlines()
    keep = {}
    for node in _defs(orig_tree):
        if node.name.startswith("_") or node.name in own:
            continue
        a, b = _span(node)
        keep[node.name] = "\n".join(orig_lines[a:b])
    if not keep:
        return new_src, []

    lines = new_src.splitlines()
    edits: list[tuple[int, int, str]] = []          # (start, end, replacement) on new lines
    restored = []
    by_name: dict[str, list] = {}
    for node in _defs(new_tree):
        if node.name in keep:
            by_name.setdefault(node.name, []).append(node)
    for name, src in keep.items():
        nodes = by_name.get(name, [])
        if len(nodes) == 1 and "\n".join(lines[_span(nodes[0])[0]:_span(nodes[0])[1]]) == src:
            continue                                 # untouched — nothing to do
        restored.append(name)
        if not nodes:
            edits.append((len(lines), len(lines), "\n\n\n" + src))
            continue
        first, *dupes = nodes
        a, b = _span(first)
        edits.append((a, b, src))
        for d in dupes:                              # a second copy would shadow the first
            a, b = _span(d)
            edits.append((a, b, ""))
    for a, b, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[a:b] = rep.split("\n") if rep else []
    out = "\n".join(lines).rstrip() + "\n"

    # Imports the restored code relied on.
    have = {ast.unparse(n) for n in ast.parse(out).body if isinstance(n, (ast.Import, ast.ImportFrom))} \
        if _parses(out) else set()
    missing = [ast.unparse(n) for n in orig_tree.body
               if isinstance(n, (ast.Import, ast.ImportFrom)) and ast.unparse(n) not in have]
    if restored and missing:
        out = "\n".join(missing) + "\n" + out
    return out, restored


def _parses(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def restore_files(task: str, written: list[str], project_root: str) -> list[str]:
    """Apply restore() to each written .py file that existed at HEAD. Returns
    'file:name' for every function put back."""
    import os
    done = []
    for rel in written:
        if not rel.endswith(".py"):
            continue
        orig = original_source(rel, project_root)
        if orig is None:
            continue
        path = os.path.join(project_root, rel)
        try:
            new = open(path).read()
        except OSError:
            continue
        out, names = restore(task, new, orig)
        if names and _parses(out):
            with open(path, "w") as f:
                f.write(out)
            done += [f"{rel}:{n}" for n in names]
    return done
