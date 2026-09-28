"""Lazily imported helper tools (autofix, the Dart import fixer, the advisor),
loaded on first use so a run that never needs them never pays their import.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""

# Lazy imports — only loaded on first validation failure to avoid startup cost
_autofix = None
_advisor = None

def _get_autofix():
    global _autofix
    if _autofix is None:
        import autofix as _autofix
    return _autofix

_dart_import_fixer = None

def _get_dart_import_fixer():
    global _dart_import_fixer
    if _dart_import_fixer is None:
        import dart_import_fixer as _dart_import_fixer
    return _dart_import_fixer

def _get_advisor():
    global _advisor
    if _advisor is None:
        import qwen_advisor as _advisor
    return _advisor
