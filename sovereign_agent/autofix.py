"""
autofix.py — Deterministic mechanical fixer for flutter analyze errors.

Runs between a failed validation and the next 35B attempt.
Handles errors that require no intelligence — just pattern matching and
precise file edits. Zero API cost, sub-second execution.

Fixable rules:
  prefer_const_constructors   → insert 'const' at the reported column
  prefer_const_declarations   → replace 'final x = const Foo()' with 'const x = Foo()'
  unused_import               → remove the import line
  depend_on_referenced_packages → add ignore_for_file header to lib/ test files
  undefined_class (dart:ui)   → add 'import dart:ui' when Color/Canvas/etc undefined
  file_names                  → add ignore_for_file: file_names to README.dart files
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

# dart:ui classes that require an explicit import
DART_UI_CLASSES = {
    'Color', 'Canvas', 'Offset', 'Paint', 'Rect', 'Size',
    'MaskFilter', 'BlendMode', 'PaintingStyle', 'BlurStyle',
    'TextStyle', 'Shadow', 'Gradient', 'ImageFilter',
}


def parse_flutter_errors(output: str) -> list[dict]:
    """Parse flutter analyze output into structured error records."""
    errors = []
    pattern = re.compile(
        r'^\s*(error|warning|info)\s+•\s+(.*?)\s+•\s+(\S+\.dart):(\d+):(\d+)\s+•\s+(\S+)',
        re.MULTILINE,
    )
    for m in pattern.finditer(output):
        errors.append({
            'level':   m.group(1),
            'message': m.group(2).strip(),
            'file':    m.group(3),
            'line':    int(m.group(4)),
            'col':     int(m.group(5)),
            'rule':    m.group(6),
        })
    return errors


# ── Individual fixers ──────────────────────────────────────────────────────────

def _read_lines(path: str) -> list[str] | None:
    try:
        return open(path).readlines()
    except Exception:
        return None


def _write_lines(path: str, lines: list[str]) -> bool:
    try:
        with open(path, 'w') as f:
            f.writelines(lines)
        return True
    except Exception:
        return False


def fix_prefer_const_constructors(file_path: str, line_no: int, col_no: int) -> bool:
    """Insert 'const ' before the constructor call at the reported position."""
    lines = _read_lines(file_path)
    if not lines or line_no < 1 or line_no > len(lines):
        return False
    line = lines[line_no - 1]
    col = col_no - 1  # convert to 0-indexed
    if col > len(line):
        return False
    # Don't double-insert
    before = line[:col].rstrip()
    if before.endswith('const'):
        return False
    lines[line_no - 1] = line[:col] + 'const ' + line[col:]
    return _write_lines(file_path, lines)


def fix_prefer_const_declarations(file_path: str, line_no: int) -> bool:
    """Replace 'final x = const Foo()' with 'const x = Foo()' on the given line."""
    lines = _read_lines(file_path)
    if not lines or line_no < 1 or line_no > len(lines):
        return False
    line = lines[line_no - 1]
    if 'final ' not in line:
        return False
    # Replace 'final' → 'const', then strip redundant 'const' from RHS
    new_line = line.replace('final ', 'const ', 1)
    new_line = re.sub(r'=\s*const\s+', '= ', new_line)
    if new_line == line:
        return False
    lines[line_no - 1] = new_line
    return _write_lines(file_path, lines)


def fix_unused_import(file_path: str, line_no: int) -> bool:
    """Delete the unused import line."""
    lines = _read_lines(file_path)
    if not lines or line_no < 1 or line_no > len(lines):
        return False
    target = lines[line_no - 1].strip()
    if not target.startswith('import '):
        return False
    lines.pop(line_no - 1)
    return _write_lines(file_path, lines)


def fix_missing_dart_ui(file_path: str, message: str) -> bool:
    """Add 'import dart:ui' if a dart:ui class appears in the error message."""
    if not any(cls in message for cls in DART_UI_CLASSES):
        return False
    content = open(file_path).read()
    if "import 'dart:ui'" in content:
        return False
    lines = content.splitlines(keepends=True)
    # Insert after the last dart: import, or at the very top
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("import 'dart:"):
            insert_at = i + 1
        elif line.startswith('import ') and insert_at == 0:
            insert_at = i  # before other imports if no dart: yet
    lines.insert(insert_at, "import 'dart:ui';\n")
    return _write_lines(file_path, lines)


def fix_depend_on_referenced_packages(file_path: str) -> bool:
    """Prepend the ignore_for_file directive for depend_on_referenced_packages."""
    content = open(file_path).read()
    if 'depend_on_referenced_packages' in content:
        return False
    with open(file_path, 'w') as f:
        f.write('// ignore_for_file: depend_on_referenced_packages\n' + content)
    return True


def fix_file_names(file_path: str) -> bool:
    """Prepend the ignore_for_file: file_names directive (for README.dart etc.)."""
    content = open(file_path).read()
    if 'file_names' in content:
        return False
    with open(file_path, 'w') as f:
        f.write('// ignore_for_file: file_names\n' + content)
    return True


# ── Dispatch table ─────────────────────────────────────────────────────────────

def _dispatch(error: dict, project_root: str) -> bool:
    rule = error['rule']
    path = os.path.join(project_root, error['file'])
    if not os.path.exists(path):
        return False

    if rule == 'prefer_const_constructors':
        return fix_prefer_const_constructors(path, error['line'], error['col'])

    if rule == 'prefer_const_declarations':
        return fix_prefer_const_declarations(path, error['line'])

    if rule == 'unused_import':
        return fix_unused_import(path, error['line'])

    if rule == 'depend_on_referenced_packages':
        return fix_depend_on_referenced_packages(path)

    if rule == 'file_names':
        return fix_file_names(path)

    if rule in ('undefined_class', 'creation_with_non_type', 'undefined_identifier'):
        return fix_missing_dart_ui(path, error['message'])

    return False


# ── Go mechanical fixer ─────────────────────────────────────────────────────────

def apply_go_mechanical_fixes(project_root: str, files_written: list[str]) -> tuple[int, list[str]]:
    """
    Deterministic fixer for Go formatting — runs `gofmt -l -w .` across the
    WHOLE project root, not just the files this task wrote.

    This has to match the scope of the validate gate: `test -z "$(gofmt -l .)"`
    scans the entire repo and is unbaselined (a Go build/format either passes
    or it doesn't, tree-wide) — there's no per-task "new errors only" filtering
    like the Flutter path has. So a single leftover misformatted file from an
    EARLIER task (very commonly: local models omitting the trailing newline at
    EOF) permanently fails every later task's validation, no matter how clean
    that task's own new file is. A fixer scoped to files_written alone cannot
    repair that pre-existing debt — it has to sweep the same scope the gate
    checks. `files_written` is accepted for logging/back-compat but no longer
    used to narrow the sweep.

    Returns (fixed_count, remaining) — remaining lists any .go files gofmt
    still flags after -w (e.g. a real syntax error it can't safely rewrite),
    so the caller knows the failure is real and not just formatting.
    """
    if shutil.which("gofmt") is None:
        return 0, []

    try:
        before = subprocess.run(
            ["gofmt", "-l", "."], capture_output=True, text=True,
            timeout=30, cwd=project_root,
        )
        flagged_before = {l.strip() for l in before.stdout.splitlines() if l.strip()}
    except Exception:
        return 0, []

    if not flagged_before:
        return 0, []

    try:
        subprocess.run(["gofmt", "-l", "-w", "."], capture_output=True,
                        timeout=30, cwd=project_root)
    except Exception:
        return 0, sorted(flagged_before)

    try:
        after = subprocess.run(
            ["gofmt", "-l", "."], capture_output=True, text=True,
            timeout=30, cwd=project_root,
        )
        remaining = {l.strip() for l in after.stdout.splitlines() if l.strip()}
    except Exception:
        remaining = flagged_before

    fixed = flagged_before - remaining
    return len(fixed), sorted(remaining)


# ── Public API ─────────────────────────────────────────────────────────────────

def apply_mechanical_fixes(analyze_output: str, project_root: str) -> tuple[int, list[dict]]:
    """
    Parse flutter analyze output and apply all deterministic mechanical fixes.

    Returns:
        (fixes_applied, unfixed_errors)
        where unfixed_errors is the list of errors that could not be auto-fixed.
    """
    errors = parse_flutter_errors(analyze_output)
    fixed_count = 0
    unfixed: list[dict] = []

    # Deduplicate: depend_on_referenced_packages fires once per import per file,
    # but we only need to fix the file header once.
    seen_dep_files: set[str] = set()
    seen_dart_ui_files: set[str] = set()

    for error in errors:
        rule = error['rule']

        # Deduplicate depend_on_referenced_packages per file
        if rule == 'depend_on_referenced_packages':
            if error['file'] in seen_dep_files:
                continue  # already fixed this file
            seen_dep_files.add(error['file'])

        # Deduplicate dart:ui injection per file
        if rule in ('undefined_class', 'creation_with_non_type'):
            if error['file'] in seen_dart_ui_files:
                continue
            if any(cls in error['message'] for cls in DART_UI_CLASSES):
                seen_dart_ui_files.add(error['file'])

        if _dispatch(error, project_root):
            fixed_count += 1
        else:
            unfixed.append(error)

    return fixed_count, unfixed
