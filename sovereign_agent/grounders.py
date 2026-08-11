"""
grounders.py — per-language grounding gates behind one interface.

Replaces the hardcoded `PROJECT_LANGUAGE == "go"` branch in work.py. That
branch is why the grounding gate — the single most effective anti-hallucination
tool in the system — only ever protected Go, while the Dart project produced
147 failing attempts, 73% of them consisting purely of invented imports and
symbols that this gate catches before a file is written.

Adding a language now means implementing this protocol and registering it.
It does not mean editing work.py.

    from grounders import for_language
    g = for_language(PROJECT_LANGUAGE)
    if g.handles(rel_path):
        content, note = g.repair(content)
        violations = g.check(rel_path, content, project_root, declared, files)

Contract notes
--------------
  - check() returns human-readable violation strings, empty list = grounded.
    The strings are fed back to the model verbatim as the next attempt's
    prompt: rejection IS the repair prompt. Keep them specific and actionable.
  - Messages must keep the `UNKNOWN IMPORT [path]:` / `UNKNOWN IDENTIFIER
    [path]:` prefixes — work.py's failure ledger categorises on them.
  - No implementation may raise. A gate that kills a run is worse than no gate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Grounder(Protocol):
    """A language's pre-write grounding gate."""

    language: str
    suffixes: tuple[str, ...]

    def handles(self, rel_path: str) -> bool:
        """True if this gate applies to the given file."""

    def repair(self, content: str) -> tuple[str, str | None]:
        """Deterministic pre-check fixes. Returns (content, note or None).

        For mechanical failures that are not worth burning an attempt on —
        Go's dropped closing braces, for example. The note, if any, is printed.
        """

    def declared_names(self, content: str) -> set[str]:
        """Names this file declares, so sibling files in the same change set
        can reference them before either is on disk."""

    def preflight(self, project_root: str) -> list[str]:
        """One-time environment problems no model can fix (unbuilt dependency
        tree, missing manifest). Empty list = environment is sane."""

    def check(self, rel_path: str, content: str, project_root: str,
              extra_declared: set[str] | None = None,
              extra_files: set[str] | None = None) -> list[str]:
        """Violation messages. Empty list = grounded. Never raises."""


# ── Shared no-op behaviour ───────────────────────────────────────────────────

class _Base:
    language = "generic"
    suffixes: tuple[str, ...] = ()

    def handles(self, rel_path: str) -> bool:
        return bool(self.suffixes) and rel_path.endswith(self.suffixes)

    def repair(self, content: str) -> tuple[str, str | None]:
        return content, None

    def declared_names(self, content: str) -> set[str]:
        return set()

    def preflight(self, project_root: str) -> list[str]:
        return []

    def check(self, rel_path, content, project_root,
              extra_declared=None, extra_files=None) -> list[str]:
        return []


class NullGrounder(_Base):
    """Used for languages with no gate yet. Explicitly does nothing, so an
    unsupported language degrades to today's behaviour rather than crashing."""
    language = "generic"


class GoGrounder(_Base):
    """Delegates to grounding.py, unchanged. That module stays the reference
    implementation — this is a thin adapter, not a rewrite."""

    language = "go"
    suffixes = (".go",)

    def repair(self, content: str) -> tuple[str, str | None]:
        import grounding
        content, added = grounding.balance_go_braces(content)
        note = (f"appended {added} missing closing brace(s)") if added else None
        return content, note

    def declared_names(self, content: str) -> set[str]:
        import grounding
        return grounding.declared_names(content)

    def preflight(self, project_root: str) -> list[str]:
        import os
        if not os.path.exists(os.path.join(project_root, "go.mod")):
            return ["no go.mod at project root — import resolution will be "
                    "degraded and every third-party import unverifiable"]
        return []

    def check(self, rel_path, content, project_root,
              extra_declared=None, extra_files=None) -> list[str]:
        import grounding
        return grounding.check_go_grounding(
            rel_path, content, project_root, extra_declared or set())


class DartGrounder(_Base):
    """Dart/Flutter gate. See dart_grounding.py."""

    language = "dart"
    suffixes = (".dart",)

    def handles(self, rel_path: str) -> bool:
        import dart_grounding
        return rel_path.endswith(".dart") and not dart_grounding.is_codegen(rel_path)

    def declared_names(self, content: str) -> set[str]:
        import dart_grounding
        return dart_grounding.declared_names(content)

    def preflight(self, project_root: str) -> list[str]:
        import dart_grounding
        return dart_grounding.preflight(project_root)

    def check(self, rel_path, content, project_root,
              extra_declared=None, extra_files=None) -> list[str]:
        import dart_grounding
        return dart_grounding.check_dart_grounding(
            rel_path, content, project_root, extra_declared, extra_files)


class TypeScriptGrounder(_Base):
    """Relative-import resolution for TS/TSX.

    Deliberately narrow. import_fixer.py already rewrites named imports before
    write and produces arity/signature hints; duplicating its symbol index here
    would mean two sources of truth. This gate covers the one thing it does not:
    an import path that points at no file at all.
    """

    language = "typescript"
    suffixes = (".ts", ".tsx")

    def handles(self, rel_path: str) -> bool:
        return rel_path.endswith(self.suffixes) and not rel_path.endswith(".d.ts")

    def preflight(self, project_root: str) -> list[str]:
        import os
        problems = []
        if not os.path.exists(os.path.join(project_root, "package.json")):
            problems.append("no package.json at project root")
        elif not os.path.isdir(os.path.join(project_root, "node_modules")):
            problems.append(
                "node_modules is absent — `npm install` has not been run, so "
                "every third-party import will fail regardless of the model")
        return problems

    def check(self, rel_path, content, project_root,
              extra_declared=None, extra_files=None) -> list[str]:
        import os
        import re
        try:
            extra = {os.path.normpath(f) for f in (extra_files or set())}
            violations = []
            pattern = re.compile(
                r"""(?:from|import)\s+['"](\.[^'"]+)['"]""")
            for spec in set(pattern.findall(content)):
                base = os.path.normpath(
                    os.path.join(os.path.dirname(rel_path), spec))
                candidates = [base + ext for ext in
                              (".ts", ".tsx", ".js", ".jsx", ".json", "")]
                candidates += [os.path.join(base, "index" + ext)
                               for ext in (".ts", ".tsx")]
                if any(os.path.exists(os.path.join(project_root, c))
                       for c in candidates):
                    continue
                if any(os.path.normpath(c) in extra for c in candidates):
                    continue
                violations.append(
                    f"UNKNOWN IMPORT [{rel_path}]: `{spec}` resolves to no file "
                    f"under {base}. Import from a module that already exists — "
                    f"do not invent module paths.")
            return violations
        except Exception as e:
            print(f"  (ts grounding gate error, skipping: {e})")
            return []


# ── Registry ─────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, Grounder] = {}


def register(grounder: Grounder, *aliases: str) -> None:
    _REGISTRY[grounder.language] = grounder
    for alias in aliases:
        _REGISTRY[alias] = grounder


register(GoGrounder(), "golang")
register(DartGrounder(), "flutter")
register(TypeScriptGrounder(), "ts", "tsx", "javascript", "js", "nextjs", "next")

_NULL = NullGrounder()


def for_language(language: str | None) -> Grounder:
    """Grounder for a language, or a no-op gate if none is registered."""
    if not language:
        return _NULL
    return _REGISTRY.get(str(language).strip().lower(), _NULL)


def supported() -> list[str]:
    return sorted({g.language for g in _REGISTRY.values()})
