"""Post-write guards: bad-pattern and error-hint matching, placeholder and
stub detection, unplanned files, and promised symbols that never appeared.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from hints import BAD_PATTERNS
from hints import ERROR_HINTS
import hints
import os
import re
import re as _re
import subprocess


_SRC_ROOTS = ("lib/", "src/", "internal/", "pkg/")


def _unplanned_new_files(written: list[str], task: str, project_root: str) -> list[str]:
    """New source files the task text never names.

    2026-08-11: task 4 needed applyMove/applyLightning/... which already
    existed in moveRules.dart, spellLightning.dart and siblings. Instead of
    importing them the model invented lib/sim/apply.dart holding twelve
    `=> []` stubs. Its gate was `flutter analyze lib`, and stubs analyze
    perfectly — so it merged green and silently broke the sim for eight
    tasks, surfacing only when a conformance gate finally ran.

    A file the roadmap never mentions is nearly always this: a fake local
    definition standing in for a real one the model did not look for.
    """
    suspects = []
    for rel in written:
        if not rel.startswith(_SRC_ROOTS):
            continue
        if os.path.basename(rel) in task or rel in task:
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=project_root, capture_output=True, text=True)
        if tracked.returncode != 0:
            suspects.append(rel)
    return suspects


_PLACEHOLDER_PROSE = re.compile(
    r"^[ \t]*(?://|#)[ \t]*(?:"
    r"implement\b|placeholder\b|todo\b|fixme\b|no changes? needed\b|"
    r"for simplicity\b|actual\b[^\n]{0,40}\blogic\b|fill (?:this |it )?in\b|"
    r"left as an exercise\b|assume |stub\b|not implemented\b|"
    r"add (?:the )?(?:real )?(?:implementation|logic)\b)", re.I | re.M)

def _decommented(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


def _all_cases_empty(src: str) -> str | None:
    """A switch whose EVERY branch is empty, decommented.

    Not "any empty branch". `case DamageTier.clean: break;` is correct — the
    spec says a clean gem draws nothing — and a guard flagging it would block
    that file at every tier forever, which is the exact false-positive shape
    that has cost this project five runs. A switch where NO branch produces
    anything is a stub; one where some do is a real implementation with a
    legitimate no-op.
    """
    body = _decommented(src)
    for m in re.finditer(r"\bswitch\s*\([^)]*\)\s*\{", body):
        depth, end = 0, len(body)
        for i in range(m.end() - 1, len(body)):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        inner = body[m.end():end]
        arms = re.split(r"\bcase\b|\bdefault\b", inner)[1:]
        if len(arms) < 2:
            continue
        stripped = [re.sub(r"^[^:]*:", "", a, count=1) for a in arms]
        if all(not re.sub(r"\bbreak\s*;|\s", "", a) for a in stripped):
            return re.sub(r"\s+", " ", inner).strip()[:70]
    return None


def _placeholder_body(written: list[str], task: str,
                      project_root: str) -> str | None:
    """A file that compiles, declares the right name, and does nothing.

    2026-08-13, the sequel. After the promised-symbol check landed, the run
    produced the SAME failure one level subtler: gem_shapes.dart declared
    `gemGeometry` with the right signature, cached correctly, and returned an
    empty `Path()` with zero facets because all three switch cases were

        case WitchType.earth:
          // Implement the path for Earth gem
          break;

    damage_overlay.dart opened with `// Placeholder implementation for the
    task`. Both merged green and were ticked done, because 47 of the 64 tasks
    are gated on `flutter analyze lib` and an empty body analyzes perfectly.

    The tell is always in the prose: the model NARRATES the hole it is leaving.
    That narration is the cheapest possible signal and it is nearly free to
    check. When it is absent this cannot help, and only a behavioural gate can
    — but it was present in all three files that got through.
    """
    m = re.search(r"\bIn ([\w./-]+\.\w+):", task)
    if not m:
        return None
    target = m.group(1)
    if target not in written:
        return None
    try:
        src = open(os.path.join(project_root, target), errors="ignore").read()
    except OSError:
        return None

    p = _PLACEHOLDER_PROSE.search(src)
    if p:
        return (f"PLACEHOLDER LEFT IN {target}: the line "
                f"`{p.group(0).strip()}` says you did not finish. A file that "
                f"compiles is not a file that works, and `flutter analyze` "
                f"cannot tell the difference — an empty function body passes it "
                f"perfectly. Write the real implementation. If you do not know "
                f"how, say so instead of emitting a shell; a task that fails "
                f"loudly costs one attempt, a hollow one that merges costs "
                f"every task downstream of it.")

    c = _all_cases_empty(src)
    if c:
        return (f"EMPTY SWITCH IN {target}: every branch of `switch {{{c}}}` "
                f"does nothing at all, so the function returns whatever it was "
                f"initialised with. Each branch the task describes must build "
                f"its own result. This analyzes clean, so nothing downstream "
                f"will catch it.")
    return None


def _missing_promised_symbol(written: list[str], task: str,
                             project_root: str) -> str | None:
    """The task named a symbol; the file it wrote does not declare it.

    2026-08-13: gem_shading.dart and gem_body.dart were each written containing
    exactly `// No changes needed for this file.` — one comment, nothing else.
    A file that is entirely a comment ANALYZES CLEAN, so the gate passed, the
    merge landed and both tasks were ticked done. gem_painter then failed at
    every tier importing paintGemBody from a file that does not define it, and
    every log blamed gem_painter, which was innocent.

    This is the same class of bug as the invented stub file, arrived at from the
    other side: there the model wrote a definition nobody asked for, here it
    wrote nothing at all where one was required. Analyze proves compilation and
    never obligation. The signature in the task text is the only machine-
    readable statement of what the task was FOR, so check it before merging.
    """
    m = re.search(r"\bIn ([\w./-]+\.\w+):\s*implement\s+`([^`]+)`", task)
    if not m:
        return None
    target, sig = m.group(1), m.group(2).strip()
    if target not in written:
        return None
    sig = re.sub(r"^(?:abstract |sealed |base |final |export |static )*"
                 r"(?:class|enum|mixin|extension|typedef|interface|func|def|"
                 r"function)\s+", "", sig)
    n = re.search(r"([A-Za-z_]\w*)\s*(?:<[^(]*>)?\s*\(", sig) or \
        re.search(r"([A-Za-z_]\w*)\s*$", sig.split("{")[0].strip())
    if not n:
        return None
    name = n.group(1)
    try:
        src = open(os.path.join(project_root, target), errors="ignore").read()
    except OSError:
        return None
    # Strip line comments so a commented-out definition cannot satisfy this.
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("//"))
    if re.search(rf"\b{re.escape(name)}\b\s*[({{=<]", body) or \
            re.search(rf"\bget\s+{re.escape(name)}\b", body):
        return None
    return (f"MISSING PROMISED SYMBOL: {target} does not declare `{name}`, which "
            f"is the entire point of this task. Writing a comment, an empty "
            f"file, or 'no changes needed' is NOT completing the task — such a "
            f"file compiles and every later file that imports `{name}` then "
            f"fails, and the blame lands on them instead of here. Write the "
            f"full definition `{sig}` with a real body.")


def apply_error_hints(error_output: str) -> str:
    """Prepend targeted hints for any recognised error patterns."""
    hints = []
    for pattern, hint in ERROR_HINTS:
        # re.search/re.match cannot take a flags argument if the pattern is already compiled.
        if isinstance(pattern, _re.Pattern):
            if pattern.search(error_output):
                hints.append(hint)
        else:
            if _re.search(pattern, error_output, _re.IGNORECASE):
                hints.append(hint)
    if hints:
        return "\n".join(hints) + "\n\n" + error_output
    return error_output


def check_bad_patterns(rel_path: str, content) -> list[str]:
    """Return list of violation messages found in content.

    Accepts str or list[str] (models sometimes return file lines as a JSON array).

    BAD_PATTERNS entries are normally a (pattern, msg) 2-tuple. An optional
    3rd element, except_paths (a tuple of rel_paths), exempts those specific
    files from that one pattern — added 2026-08-22 after projectsdash's own
    "child_process" guard got stuck blocking its OWN intended exception: the
    hint text said "Only lib/ops/spawn.ts is allowed to import
    child_process", but nothing enforced that — the pattern matched every
    file unconditionally, including spawn.ts itself, whose entire job is to
    call child_process.spawn(). That is a permanently unsatisfiable gate: an
    attempt that correctly implements the task is guaranteed to trip its own
    guard, every time, forever (see xanadu-run-control's "whose failure is
    it" diagnostic — the file the guard names IS the file the task targets).
    2-tuples are unaffected; this is purely additive.
    """
    if isinstance(content, list):
        content = "\n".join(str(line) for line in content)
    elif not isinstance(content, str):
        content = str(content)
    violations = []
    for entry in BAD_PATTERNS:
        pattern, msg = entry[0], entry[1]
        except_paths = entry[2] if len(entry) > 2 else ()
        if rel_path in except_paths:
            continue
        if isinstance(pattern, _re.Pattern):
            if pattern.search(content):
                violations.append(f"[{rel_path}] {msg}")
        else:
            if _re.search(pattern, content, _re.MULTILINE):
                violations.append(f"[{rel_path}] {msg}")
    return violations
