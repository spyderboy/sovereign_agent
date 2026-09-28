"""Read-only API stubs of locked files (Dart, TypeScript, Python) for
worker prompts.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
import os
import re as _re


def _extract_dart_api_stub(content: str, max_chars: int = 800) -> str:
    """Extract a compact read-only API stub from a Dart source file.

    Returns class declarations, constructor signatures, and public field/method
    signatures — enough for the model to use the API correctly without seeing
    the full implementation.  Private members (_name) are omitted.
    """
    import re as _re
    lines = content.splitlines()
    stub_lines: list[str] = []
    in_class = False
    brace_depth = 0

    for line in lines:
        stripped = line.strip()

        # Skip private members
        if _re.match(r'(final|late|var|void|Future|List|Map|Set|int|double|bool|String)\s+_', stripped):
            continue
        if _re.match(r'_\w+\(', stripped):
            continue

        # Class declaration
        if _re.match(r'(abstract\s+)?class\s+\w+', stripped):
            in_class = True
            brace_depth = 0
            stub_lines.append(line.rstrip())
            continue

        if in_class:
            brace_depth += stripped.count('{') - stripped.count('}')
            if brace_depth < 0:
                stub_lines.append('}')
                in_class = False
                brace_depth = 0
                continue

            # Only emit top-level members (brace_depth == 1 means we're directly inside the class)
            if brace_depth != 1:
                continue
            # Skip comments, blank lines, control-flow keywords
            if not stripped or stripped.startswith('//') or stripped.startswith('/*'):
                continue
            if _re.match(r'(if|else|for|while|switch|return|throw|assert|case|default|break|continue)\b', stripped):
                continue

            # Constructor: ClassName( — top-level, no return type
            if _re.match(r'[A-Z]\w*[\._]?\w*\s*\(', stripped):
                stub_lines.append('  ' + stripped.split('{')[0].split('=>')[0].strip() + ';')
            # Public field declaration
            elif _re.match(r'(final\s+|late\s+|static\s+const\s+|static\s+final\s+|static\s+)?(int|double|bool|String|var|dynamic|[A-Z]\w*)[?<\s]', stripped):
                decl = stripped.split('=')[0].strip().rstrip(';') + ';'
                if not decl.startswith('_'):
                    stub_lines.append('  ' + decl)
            # Public method / getter signature (return type + name)
            elif _re.match(r'(static\s+|override\s+)?(void|Future|bool|int|double|String|List|Map|Set|[A-Z]\w*)[?<\s].*[a-z]\w*\s*[\(\{]', stripped):
                sig = stripped.split('{')[0].split('=>')[0].strip()
                if not _re.match(r'_', sig.split()[-1] if ' ' in sig else sig):
                    stub_lines.append('  ' + sig + ' { ... }')
            # enum
            elif _re.match(r'enum\s+\w+', stripped):
                stub_lines.append(line.rstrip())

    result = '\n'.join(stub_lines)
    return result[:max_chars] if len(result) > max_chars else result


def _ts_fn_sig(lines: list[str], i: int) -> tuple[str, int]:
    """Signature (body dropped) for an exported function starting at line i.

    Matches the parameter parens across lines and cuts at the body brace, so
    inline object-typed params (e.g. `isIdle(u: { target: Vec2 | null })`) and
    multi-line signatures survive intact. Returns (signature, next_index).
    """
    buf = lines[i]
    depth = buf.count('(') - buf.count(')')
    j = i
    while depth > 0 and j + 1 < len(lines):
        j += 1
        buf += ' ' + lines[j].strip()
        depth += lines[j].count('(') - lines[j].count(')')
    p = buf.find('(')
    close, d = -1, 0
    for idx in range(p, len(buf)):
        if buf[idx] == '(':
            d += 1
        elif buf[idx] == ')':
            d -= 1
            if d == 0:
                close = idx
                break
    after = buf[close + 1:] if close != -1 else ''
    b = after.find('{')
    ret = after[:b] if b != -1 else after
    sig = (buf[:close + 1] if close != -1 else buf) + ret
    return sig.strip() + ' { ... }', j + 1


def _extract_ts_api_stub(content: str, max_chars: int = 1500) -> str:
    """Read-only API surface of a TypeScript/JavaScript file.

    Keeps exported function/class signatures and FULL interface / object-type /
    enum bodies (their fields ARE the API the model must use) plus full
    const/let/type-alias lines (constant VALUES like MOTE_SPEED = 60 matter).
    Function bodies are dropped to save budget.
    """
    import re as _re
    lines = content.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        # Exported interface / enum / object-type alias: keep the whole brace block.
        if (_re.match(r'export\s+(interface|enum)\b', s)
                or _re.match(r'export\s+type\s+\w+[^=]*=\s*\{', s)):
            block = [lines[i].rstrip()]
            depth = s.count('{') - s.count('}')
            i += 1
            while i < n and depth > 0:
                block.append(lines[i].rstrip())
                depth += lines[i].count('{') - lines[i].count('}')
                i += 1
            out.append("\n".join(block))
            continue
        # Exported function: signature only.
        if _re.match(r'export\s+(async\s+)?function\b', s):
            sig, i = _ts_fn_sig(lines, i)
            out.append(sig)
            continue
        # Exported class: declaration head only.
        if _re.match(r'export\s+(abstract\s+)?class\b', s):
            out.append(s.split('{')[0].strip() + ' { ... }')
            i += 1
            continue
        # Exported const / let / one-line type alias: keep the full line.
        if _re.match(r'export\s+(const|let|type)\b', s):
            out.append(s.rstrip())
            i += 1
            continue
        i += 1
    return "\n".join(out).strip()[:max_chars]


def _extract_api_stub(path: str, content: str, max_chars: int = 1500) -> str:
    """Language-aware read-only API stub for a locked file, dispatched by extension.

    2026-07-16: locked files were run unconditionally through the Dart
    extractor, so on TypeScript projects (GalaxicanJS) vec.ts / types.ts
    produced an EMPTY stub — the model never saw `dist`, `pos: Vec2`, or
    `interface Mote`, and hallucinated `vecDist`, `.position`, `sortedIds`
    on nearly every task, escalating both A/B arms for the same reason.
    Markdown (API.md) is injected whole — never code-parsed.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".md":
        return content[:max_chars]
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
        return _extract_ts_api_stub(content, max_chars=max_chars)
    if ext == ".py":
        return _extract_py_api_stub(content, max_chars=max_chars)
    return _extract_dart_api_stub(content, max_chars=min(max_chars, 800))


def _extract_py_api_stub(content: str, max_chars: int = 1500) -> str:
    """Read-only API surface of a Python file, via the ast module.

    2026-09-28: .py files fell through to the Dart extractor and produced an
    empty stub — the GalaxicanJS failure mode (model never sees the locked
    types, invents the API). Keeps module constants WITH values, classes /
    dataclasses with their annotated fields and enum members, and every
    function / method signature (names, annotations, defaults) with its first
    docstring line. Bodies are dropped. Unparseable source is returned raw.
    """
    import ast as _ast

    try:
        tree = _ast.parse(content)
    except SyntaxError:
        return content[:max_chars]

    def _sig(fn, indent: str) -> str:
        prefix = "async def" if isinstance(fn, _ast.AsyncFunctionDef) else "def"
        ret = f" -> {_ast.unparse(fn.returns)}" if fn.returns else ""
        decos = "".join(f"{indent}@{_ast.unparse(d)}\n" for d in fn.decorator_list)
        doc = _ast.get_docstring(fn)
        note = f"  # {doc.strip().splitlines()[0]}" if doc else ""
        return f"{decos}{indent}{prefix} {fn.name}({_ast.unparse(fn.args)}){ret}: ...{note}"

    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            continue
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                out.append(_sig(node, ""))
        elif isinstance(node, _ast.ClassDef):
            decos = "".join(f"@{_ast.unparse(d)}\n" for d in node.decorator_list)
            bases = f"({', '.join(_ast.unparse(b) for b in node.bases)})" if node.bases else ""
            lines = [f"{decos}class {node.name}{bases}:"]
            for item in node.body:
                if isinstance(item, (_ast.AnnAssign, _ast.Assign)):
                    lines.append(f"    {_ast.unparse(item)}")
                elif isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    if not item.name.startswith("_") or item.name in ("__init__", "__post_init__"):
                        lines.append(_sig(item, "    "))
            if len(lines) == 1:
                lines.append("    ...")
            out.append("\n".join(lines))
        elif isinstance(node, (_ast.Assign, _ast.AnnAssign)):
            out.append(_ast.unparse(node))
        elif type(node).__name__ == "TypeAlias":  # `type X = ...` (3.12+)
            out.append(_ast.unparse(node))

    return "\n\n".join(out)[:max_chars]
