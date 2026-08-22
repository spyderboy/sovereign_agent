"""Deterministic TypeScript import repair for the sovereign worker loop.

The single most common local-model failure across every stack is the
"invented import" — the model references a real helper but imports it from
the wrong module (`sortedIds` from './types' instead of './state'), or
imports a module that doesn't exist at all (`./util`). Feeding whole files
as context does not fix it; the model still has to infer symbol->module.

This module makes imports deterministic instead of guessed:

  * build_symbol_index() scans the project's source tree for every exported
    symbol and records which module it lives in.
  * fix_ts_imports() rewrites a generated file's relative named-imports so
    each symbol is imported from its real module, and drops symbols/modules
    that don't exist. Runs BEFORE tsc, so the file typechecks without the
    model ever getting the import right.
  * build_import_map() renders a compact "import these from here" map for the
    coding prompt, with the exact relative specifier for the target file.
  * import_error_hint() turns a tsc TS2305/TS2307 failure into a precise
    correction line for the retry prompt.

Language-agnostic in spirit; this implementation targets .ts/.tsx.
"""

from __future__ import annotations

import os
import re

# export function/const/let/var/class/interface/type/enum NAME
_EXPORT_DECL_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function\*?|const|let|var|class|abstract\s+class|interface|type|enum|namespace)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.M,
)
# export { a, b as c } ...   (grouped / re-exports)
_EXPORT_LIST_RE = re.compile(r"^\s*export\s*(?:type\s+)?\{([^}]*)\}", re.M)

# import { a, type B, c as d } from './mod';   (named imports only)
_NAMED_IMPORT_RE = re.compile(
    r"import\s+(?P<typeonly>type\s+)?\{(?P<body>[^}]*)\}\s*from\s*"
    r"['\"](?P<spec>[^'\"]+)['\"]\s*;?[ \t]*\n?",
)


def _slashes(p: str) -> str:
    return p.replace(os.sep, "/")


def _bare_name(token: str) -> str:
    """The exported identifier a token refers to: 'type Foo as Bar' -> 'Foo'."""
    t = token.strip()
    if t.startswith("type "):
        t = t[5:].strip()
    return t.split(" as ")[0].strip()


def build_symbol_index(project_root: str, src_rel: str = "src"):
    """Return (symbol -> module_relpath, module_relpath -> set(exported symbols)).

    module_relpath is relative to project_root, forward-slashed, no extension
    (e.g. 'src/sim/state'), matching how an import specifier resolves.
    """
    index: dict[str, str] = {}
    exports: dict[str, set[str]] = {}
    root = os.path.join(project_root, src_rel)
    if not os.path.isdir(root):
        for candidate in ("lib", "."):
            root = os.path.join(project_root, candidate)
            if os.path.isdir(root):
                break
        else:
            return index, exports
    _SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".venv", "venv"}
    for dirpath, _dirs, files in os.walk(root):
        _dirs[:] = [d for d in _dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not fn.endswith((".ts", ".tsx")) or fn.endswith(".d.ts"):
                continue
            full = os.path.join(dirpath, fn)
            modrel = _slashes(os.path.relpath(full, project_root))
            modrel = re.sub(r"\.tsx?$", "", modrel)
            try:
                text = open(full, encoding="utf-8").read()
            except Exception:
                continue
            syms: set[str] = set(_EXPORT_DECL_RE.findall(text))
            for m in _EXPORT_LIST_RE.finditer(text):
                for part in m.group(1).split(","):
                    name = part.strip()
                    if not name or "from" in name:
                        continue
                    # export { a as b } exposes b
                    exposed = name.split(" as ")[-1].strip().lstrip("type ").strip()
                    if exposed:
                        syms.add(exposed)
            syms.discard("")
            exports.setdefault(modrel, set()).update(syms)
            for s in syms:
                index.setdefault(s, modrel)  # first definition wins
    return index, exports


_FUNC_START_RE = re.compile(r"export\s+(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(?=[<(])")


def _balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index of the close char matching the open char at `start`, or -1."""
    depth = 0
    j = start
    n = len(text)
    while j < n:
        c = text[j]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _extract_one_signature(text: str, pos: int, name: str) -> str | None:
    """Extract a full `name<G>(params): Ret` signature starting just after name."""
    n = len(text)
    i = pos
    while i < n and text[i] in " \t\n":
        i += 1
    generics = ""
    if i < n and text[i] == "<":
        end = _balanced(text, i, "<", ">")
        if end == -1:
            return None
        generics = text[i:end + 1]
        i = end + 1
    while i < n and text[i] in " \t\n":
        i += 1
    if i >= n or text[i] != "(":
        return None
    close = _balanced(text, i, "(", ")")
    if close == -1:
        return None
    params = text[i:close + 1]
    ret = ""
    mret = re.match(r"\s*:\s*([^{;]+?)\s*\{", text[close + 1:close + 400])
    if mret:
        ret = mret.group(1).strip()
    sig = f"{name}{generics}{params}" + (f": {ret}" if ret else "")
    return re.sub(r"\s+", " ", sig).strip()


def build_signature_index(project_root: str, src_rel: str = "src") -> dict[str, str]:
    """Map exported function name -> full call signature string, e.g.
    'productionTick(g: GameState, sdt: number, rng: Rng): void'."""
    sigs: dict[str, str] = {}
    root = os.path.join(project_root, src_rel)
    if not os.path.isdir(root):
        for candidate in ("lib", "."):
            root = os.path.join(project_root, candidate)
            if os.path.isdir(root):
                break
        else:
            return sigs
    _SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".venv", "venv"}
    for dirpath, _dirs, files in os.walk(root):
        _dirs[:] = [d for d in _dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not fn.endswith((".ts", ".tsx")) or fn.endswith(".d.ts"):
                continue
            try:
                text = open(os.path.join(dirpath, fn), encoding="utf-8").read()
            except Exception:
                continue
            for m in _FUNC_START_RE.finditer(text):
                sig = _extract_one_signature(text, m.end(), m.group(1))
                if sig:
                    sigs.setdefault(m.group(1), sig)
    return sigs


def _resolve_spec(importer_relpath: str, spec: str) -> str | None:
    """Resolve a relative import spec to a module_relpath, or None if not relative."""
    if not spec.startswith("."):
        return None
    base = os.path.dirname(importer_relpath)
    return _slashes(os.path.normpath(os.path.join(base, spec)))


def _spec_from(importer_relpath: str, target_modrel: str) -> str:
    """Relative specifier to import target_modrel from importer_relpath's dir."""
    base = os.path.dirname(importer_relpath)
    rel = _slashes(os.path.relpath(target_modrel, base))
    if not rel.startswith("."):
        rel = "./" + rel
    return rel


def fix_ts_imports(project_root: str, written_files: list[str], index=None, exports=None):
    """Rewrite relative named-imports in each written .ts file so every symbol
    is imported from its true module, drop symbols that exist nowhere, and add
    an import for any known project symbol that's used but never imported at
    all (the model referenced a real helper as a bare identifier and wrote no
    import statement for it whatsoever).

    Returns a list of human-readable notes describing what was corrected.
    Safe: on any parse trouble a file is left untouched.
    """
    if index is None or exports is None:
        index, exports = build_symbol_index(project_root)
    notes: list[str] = []
    for rel in written_files:
        if not rel.endswith((".ts", ".tsx")) or rel.endswith(".d.ts"):
            continue
        full = os.path.join(project_root, rel)
        try:
            original = open(full, encoding="utf-8").read()
        except Exception:
            continue
        new_text, changed = _rewrite_file(_slashes(rel), original, index, exports, notes)
        if changed and new_text != original:
            try:
                open(full, "w", encoding="utf-8").write(new_text)
            except Exception:
                pass
    return notes


_LOCAL_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:function\*?|const|let|var|class|abstract\s+class|interface|type|enum|namespace)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.M,
)


def _bound_import_names(text: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(r"^\s*import\s+(?:type\s+)?(.+?)\s+from\s+['\"][^'\"]+['\"]", text, re.M):
        clause = m.group(1).strip()
        ns = re.match(r"\*\s+as\s+([A-Za-z_$][\w$]*)", clause)
        if ns:
            names.add(ns.group(1))
            continue
        brace = re.search(r"\{([^}]*)\}", clause)
        before = clause[:brace.start()] if brace else clause
        default_name = before.strip().rstrip(",").strip()
        if default_name and re.match(r"^[A-Za-z_$][\w$]*$", default_name):
            names.add(default_name)
        if brace:
            for part in brace.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                if part.startswith("type "):
                    part = part[5:].strip()
                bare = part.split(" as ")[-1].strip()
                if bare:
                    names.add(bare)
    return names


def _rewrite_file(rel: str, text: str, index, exports, notes: list[str]):
    # desired[modrel] = {bare: token_text}   (relative, local symbols only)
    desired: dict[str, dict[str, str]] = {}
    changed = False
    first_pos = [None]  # position to insert the consolidated block

    def collect(m: re.Match) -> str:
        nonlocal changed
        spec = m.group("spec")
        target = _resolve_spec(rel, spec)
        if target is None:
            return m.group(0)  # package import — leave untouched
        if first_pos[0] is None:
            first_pos[0] = m.start()
        type_prefix = "type " if m.group("typeonly") else ""
        for raw in m.group("body").split(","):
            tok = raw.strip()
            if not tok:
                continue
            bare = _bare_name(tok)
            correct = index.get(bare)
            token_text = (type_prefix + tok) if type_prefix and not tok.startswith("type ") else tok
            if correct is None:
                # symbol exists nowhere — drop it
                changed = True
                notes.append(f"{rel}: dropped unknown import '{bare}'")
                continue
            if correct != target:
                changed = True
                notes.append(f"{rel}: '{bare}' import moved {spec} -> {_spec_from(rel, correct)}")
            desired.setdefault(correct, {}).setdefault(bare, token_text)
        return ""  # remove this statement; consolidated block re-emitted later

    body_removed = _NAMED_IMPORT_RE.sub(collect, text)

    own_modrel = re.sub(r"\.tsx?$", "", rel)
    known = {b for mod_bares in desired.values() for b in mod_bares}
    known |= _bound_import_names(text)
    known |= set(_LOCAL_DECL_RE.findall(body_removed))
    for name, modrel in index.items():
        if name in known or modrel == own_modrel:
            continue
        if re.search(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", body_removed):
            desired.setdefault(modrel, {}).setdefault(name, name)
            known.add(name)
            changed = True
            notes.append(f"{rel}: added missing import '{name}' from {_spec_from(rel, modrel)}")

    if not changed:
        return text, False

    # Re-emit consolidated, correct imports, sorted for determinism.
    lines = []
    for modrel in sorted(desired):
        toks = list(desired[modrel].values())
        # type-only if every token carries the type keyword
        all_type = all(t.strip().startswith("type ") for t in toks)
        if all_type:
            toks = [t.strip()[5:].strip() for t in toks]
            lines.append(f"import type {{ {', '.join(sorted(toks))} }} from '{_spec_from(rel, modrel)}';")
        else:
            lines.append(f"import {{ {', '.join(sorted(toks))} }} from '{_spec_from(rel, modrel)}';")
    block = "\n".join(lines)

    insert_at = first_pos[0] if first_pos[0] is not None else 0
    # first_pos indexed into the ORIGINAL text; recompute against body_removed by
    # re-running: simplest robust approach is to place the block just before the
    # first remaining non-empty line that isn't a comment/blank at file top.
    return _insert_block(body_removed, block), True


def _insert_block(text: str, block: str) -> str:
    """Insert the consolidated import block after the leading header comment,
    before the first code line."""
    lines = text.split("\n")
    i = 0
    n = len(lines)
    # skip leading blank lines and // comments and /* */ blocks
    while i < n:
        s = lines[i].strip()
        if s == "" or s.startswith("//"):
            i += 1
            continue
        break
    out = lines[:i] + [block] + lines[i:]
    result = "\n".join(out)
    # collapse any run of 3+ blank lines the removals may have left
    return re.sub(r"\n{3,}", "\n\n", result)


def build_import_map(project_root: str, target_relpath: str, index=None, exports=None, sigs=None) -> str:
    """Compact 'import these from here, call them like this' map for the coding
    prompt, with the exact relative specifier for target_relpath. Function
    symbols are rendered with their full signature so the model calls them with
    the right argument count and types; other symbols show just their name."""
    if index is None or exports is None:
        index, exports = build_symbol_index(project_root)
    if sigs is None:
        sigs = build_signature_index(project_root)
    target = _slashes(target_relpath)
    lines = []
    for modrel in sorted(exports):
        if _slashes(modrel) == re.sub(r"\.tsx?$", "", target):
            continue  # don't list the file being written
        syms = sorted(exports[modrel])
        if not syms:
            continue
        spec = _spec_from(target, modrel)
        rendered = [sigs.get(s, s) for s in syms]
        lines.append(f"  '{spec}': {', '.join(rendered)}")
    if not lines:
        return ""
    return (
        "IMPORT & SIGNATURE MAP — import each symbol from EXACTLY the module "
        "shown (paths are already relative to the file you are writing), and "
        "call each function with EXACTLY the signature shown (mind the argument "
        "COUNT and TYPES — they are NOT uniform). Do NOT invent modules, move "
        "symbols between modules, or guess argument lists:\n"
        + "\n".join(lines)
    )


_ARITY_RE = re.compile(r"([\w./-]+\.tsx?)\((\d+),(\d+)\):\s*error TS(?:2554|2345)")


def signature_error_hint(output: str, project_root: str, sigs=None) -> str:
    """Turn tsc arity/arg-type errors (TS2554/TS2345) into the correct call
    signatures of the functions referenced on the offending lines."""
    if sigs is None:
        sigs = build_signature_index(project_root)
    hints: list[str] = []
    seen: set[str] = set()
    for path, line, _col in _ARITY_RE.findall(output):
        full = os.path.join(project_root, path)
        try:
            src_lines = open(full, encoding="utf-8").read().split("\n")
            src_line = src_lines[int(line) - 1]
        except Exception:
            continue
        for name in re.findall(r"([A-Za-z_$][\w$]*)\s*\(", src_line):
            if name in sigs and name not in seen:
                seen.add(name)
                hints.append(f"  {sigs[name]}")
    if not hints:
        return ""
    return ("CALL SIGNATURES (call these EXACTLY — the argument count/types are "
            "not uniform across ticks):\n" + "\n".join(hints))


_TS2305_RE = re.compile(r"Module\s+'\"?([^'\"]+?)\"?'\s+has no exported member '([^']+)'")
_TS2307_RE = re.compile(r"Cannot find module '([^']+)'")


def import_error_hint(output: str, project_root: str, index=None, exports=None) -> str:
    """Turn tsc import errors into precise corrections for the retry prompt."""
    if index is None or exports is None:
        index, exports = build_symbol_index(project_root)
    hints: list[str] = []
    for badmod, sym in _TS2305_RE.findall(output):
        correct = index.get(sym)
        if correct:
            hints.append(f"  '{sym}' is exported from '{os.path.basename(correct)}', "
                         f"not '{badmod}'. Import it from the correct module.")
        else:
            hints.append(f"  '{sym}' is not exported anywhere — do not import it; "
                         f"define it locally or use a real helper.")
    for badmod in _TS2307_RE.findall(output):
        hints.append(f"  Module '{badmod}' does not exist. Do not import from it.")
    if not hints:
        return ""
    return "IMPORT FIXES (apply exactly):\n" + "\n".join(dict.fromkeys(hints))
