"""
prompt_artifacts.py — grounding gate for model-generated text that enters the
prompt path.

WHY THIS EXISTS
---------------
2026-07-14: an auto-promoted .roorules entry told models to call `.IsNeutral()`,
a method that exists nowhere in the project. The RULE was the hallucination.
Every subsequent prompt then primed models to write banned code — roughly 30
blocked attempts before a human caught it.

The lesson, recorded in promote_rules.py at the time:

    Rules drafted by a model must pass the same grounding bar as code
    written by one.

That principle was only ever enforced at one site (rule promotion). This module
generalises it so every path that writes model-generated text into a future
prompt can share one verifier:

    promote_rules.py   — candidate .roorules entries      (mode="reject")
    work.py            — advisor enriched_hint            (mode="reject")
    dream.py           — drafted .roorules / VISION.md    (mode="warn")

WHAT IT CHECKS
--------------
1. Grounding      — does the text reference identifiers that exist nowhere in
                    the project source, the language's stdlib, or the framework
                    allowlist? (The .IsNeutral case.)
2. Foreign vocab  — does the text speak another stack's language? A rule
                    mentioning Riverpod has no business in a Go prompt.
                    (Generalises promote_rules._FOREIGN_LANGUAGE_MARKERS, which
                    was hardcoded one-directional: Go rejecting Dart only.)

DESIGN CONSTRAINTS
------------------
- Never raises. A verifier that crashes a run is worse than one that misses.
  Every check is individually wrapped; failures downgrade to "not checked".
- Never blocks on a weak whitelist. If we cannot build a confident picture of
  what identifiers legitimately exist (empty project, unsupported language),
  grounding reports `not_checked` rather than rejecting. False rejections on a
  greenfield project would make dream.py unusable.
- The cost of a false positive is one extra model attempt with an explanatory
  message. The cost of a false negative is a poisoned prompt for every task
  that follows. Tune accordingly — but not so far that the gate cries wolf.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── Language specs ───────────────────────────────────────────────────────────
# `globs`      : source files scanned to build the project identifier whitelist
# `vocab`      : regexes that identify text as speaking THIS language's dialect
# `frameworks` : identifiers from the framework/SDK that legitimately appear in
#                rules but will not be found in project source

LANGUAGE_ALIASES = {
    "go": "go", "golang": "go",
    "dart": "dart", "flutter": "dart",
    "typescript": "typescript", "ts": "typescript", "tsx": "typescript",
    "javascript": "typescript", "js": "typescript", "nextjs": "typescript",
    "next": "typescript", "react": "typescript",
    "python": "python", "py": "python",
    "swift": "swift", "swiftui": "swift",
}

LANGUAGE_SPECS: dict[str, dict] = {
    "go": {
        "globs": ["*.go"],
        "vocab": [
            r"\bgofmt\b", r"\bgo\s+vet\b", r"\bgoroutine\b", r"\.go\b",
            r"interface\{\}", r"err\s*!=\s*nil", r"\bpackage\s+main\b",
            r"\bgo\.mod\b", r"\bGOROOT\b", r"\bGOPATH\b",
        ],
        "frameworks": set(),  # stdlib comes from grounding.stdlib_whitelist()
    },
    "dart": {
        "globs": ["*.dart"],
        "vocab": [
            r"\.dart\b", r"\bflutter\b", r"\briverpod\b", r"\bwidget\b",
            r"\bpubspec\b", r"\bStateNotifier\b", r"\bBuildContext\b",
            r"\bpub\.dev\b", r"\bFlame\b", r"\bVector2\b", r"\bdart:\w+",
        ],
        "frameworks": {
            "BuildContext", "StatelessWidget", "StatefulWidget", "MaterialApp",
            "Scaffold", "AppBar", "ThemeData", "MediaQuery", "Navigator",
            "GoRouter", "ChangeNotifier", "ValueNotifier", "FutureBuilder",
            "StreamBuilder", "AsyncValue", "ConsumerWidget", "WidgetRef",
            "NotifierProvider", "StateProvider", "FutureProvider", "Notifier",
            "PositionComponent", "SpriteComponent", "Vector2", "FlameGame",
            "CustomPainter", "EdgeInsets", "BoxDecoration", "TextStyle",
            "GestureDetector", "SingleChildScrollView", "ListView", "SizedBox",
            # lowerCamelCase framework members
            "setState", "initState", "removeFromParent", "addToParent",
            "didChangeDependencies", "createState", "toOffset", "toVector2",
            "runApp", "watchProvider", "readProvider", "onLoad", "onMount",
            "notifyListeners", "copyWith", "toJson", "fromJson", "toString",
        },
    },
    "typescript": {
        "globs": ["*.ts", "*.tsx", "*.js", "*.jsx"],
        "vocab": [
            r"\.tsx?\b", r"\btypescript\b", r"\bnext\.js\b", r"\breact\b",
            r"\buseEffect\b", r"\btsconfig\b", r"\bnpm\b", r"package\.json",
            r"\"use client\"", r"\bServer Component\b", r"\bApp Router\b",
            r"\btailwind\b", r"\bnode_modules\b",
        ],
        "frameworks": {
            "React", "ReactNode", "NextRequest", "NextResponse", "NextPage",
            "useState", "useEffect", "useMemo", "useCallback", "useRef",
            "useContext", "useReducer", "ServerComponent", "GetServerSideProps",
            "Promise", "Record", "Partial", "Omit", "Pick", "Awaited",
            "JSX", "HTMLElement", "PropsWithChildren", "Dispatch", "SetStateAction",
        },
    },
    "python": {
        "globs": ["*.py"],
        "vocab": [
            r"\.py\b", r"\bpytest\b", r"\bpydantic\b", r"\b__init__\b",
            r"\bpip\b", r"requirements\.txt", r"\bdataclass\b", r"\bf-string\b",
            r"\bvirtualenv\b", r"\bvenv\b",
        ],
        "frameworks": {
            "BaseModel", "Optional", "Union", "Sequence", "Mapping", "Iterable",
            "Callable", "Awaitable", "TypeVar", "Enum", "Path", "Decimal",
            "FastAPI", "APIRouter", "HTTPException", "Depends", "Field",
            "ValueError", "TypeError", "KeyError", "RuntimeError", "Exception",
        },
    },
    "swift": {
        "globs": ["*.swift"],
        "vocab": [
            r"\.swift\b", r"\bswiftui\b", r"\bxcode\b", r"@Observable\b",
            r"\bNavigationStack\b", r"\bSPM\b", r"\bxcconfig\b", r"\bUIKit\b",
        ],
        "frameworks": {
            "NavigationStack", "NavigationLink", "ObservableObject", "StateObject",
            "EnvironmentObject", "URLSession", "URLProtocol", "Codable",
            "Decodable", "Encodable", "XCTestCase", "SwiftUI", "Foundation",
            "MainActor", "TaskGroup", "AsyncSequence", "Published", "ViewBuilder",
        },
    },
    "generic": {"globs": [], "vocab": [], "frameworks": set()},
}

# Identifiers that show up in prose rules regardless of stack. Never flag these.
UNIVERSAL_ALLOWLIST = {
    "TODO", "FIXME", "JSON", "YAML", "HTTP", "HTTPS", "API", "URL", "URI",
    "README", "CHANGELOG", "VISION", "ROADMAP", "MUST", "NEVER", "ALWAYS",
    "CI", "CD", "PR", "SDK", "CLI", "UI", "UX", "DTO", "ORM", "CRUD",
    "GitHub", "GitLab", "OAuth", "JavaScript", "TypeScript", "SQLite",
    "PostgreSQL", "MySQL", "Firebase", "Firestore", "CloudFunctions",
    "KeepAChangelog", "MoveEffect", "RotateEffect", "ScaleEffect",
}

# Minimum project-token count before grounding is trusted enough to reject on.
# Below this we cannot distinguish "identifier does not exist" from "we failed
# to read the project", so we decline to judge.
MIN_WHITELIST_TOKENS = 200

# Candidate-identifier extraction.
# The first two patterns come straight from the 2026-07-14 post-mortem: the
# dot-only check let three poisonous rules through because their identifiers
# appeared without a leading dot (FindClosestStarToPos, StateNotifier).
_DOT_SELECTOR = re.compile(r"\.([A-Z]\w{2,})\b")
_MULTI_HUMP = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b")

# Go exports are always capitalized, so the original gate could stop there.
# Dart, TypeScript and Swift methods are lowerCamelCase, and a rule like
# "call removeFromParent() before spawning the effect" would otherwise sail
# through on every non-Go project. Restricted to call/selector position and to
# names with at least one hump, so ordinary English prose never matches.
_LOWER_CAMEL = re.compile(r"\b([a-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+)\b")
_LOWER_CAMEL_CALL = re.compile(r"\b([a-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+)\s*\(")
_LOWER_CAMEL_SELECTOR = re.compile(r"\.([a-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+)\b")

_CAP_TOKEN = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")

# Fenced code blocks in a rule are illustrations ("FORBIDDEN — stub with
# comment body"), not instructions. Their identifiers are deliberately fake.
# Accepted limit: a hallucinated identifier hiding inside a fenced example
# goes unchecked. Callers that pass raw model output rather than a written
# rule should set check_code_blocks=True.
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)

# Prohibition context. A rule that says "never use StateNotifier — removed in
# Riverpod 2.x" MUST name an identifier that does not exist; that is the whole
# point of the rule. Flagging it would make the gate reject exactly the rules
# most worth keeping. Checked against the text immediately preceding the
# identifier's first occurrence.
_PROHIBITION = re.compile(
    r"(?:never|do\s+not|don't|avoid|forbidden|deprecated|removed|"
    r"no\s+longer|instead\s+of|rather\s+than|replaced\s+by|not\s+exist|"
    r"stop\s+using|banned)\b"
    # Stay inside the sentence, but allow the dot in a qualified name
    # (Galaxy.LegacyStep) — a plain `[^.]` window ends at that dot and misses
    # the very identifier the prohibition is about.
    r"(?:[^.\n]|\.(?=\w)){0,60}$",
    re.IGNORECASE,
)
_PROHIBITION_WINDOW = 80

_SKIP_DIRS = {".git", "build", "node_modules", "logs", ".dart_tool", ".venv",
              "vendor", "dist", ".next", "Pods", "__pycache__"}

_project_cache: dict[tuple[str, str], tuple[float, set[str]]] = {}
_PROJECT_CACHE_TTL = 30.0


# ── Verdict ──────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    """Result of verifying one model-generated artifact.

    ok        — safe to write into the prompt path
    reasons   — why it was rejected (empty if ok)
    warnings  — advisory notes that did not cause rejection
    checked   — which checks actually ran ("grounding", "foreign_vocab")
    skipped   — checks that could not run, with why
    """
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "ok"
        parts = list(self.reasons) + [f"(warn) {w}" for w in self.warnings]
        return "; ".join(parts) if parts else "ok"


# ── Language resolution ──────────────────────────────────────────────────────

def normalise_language(lang: str | None) -> str:
    if not lang:
        return "generic"
    return LANGUAGE_ALIASES.get(str(lang).strip().lower(), "generic")


def project_language(project_root: str) -> str:
    """Read `language` from .sovereign_config.json. Falls back to 'generic'."""
    try:
        cfg_path = os.path.join(project_root, ".sovereign_config.json")
        with open(cfg_path) as f:
            return normalise_language(json.load(f).get("language"))
    except Exception:
        return "generic"


# ── Whitelist construction ───────────────────────────────────────────────────

def project_whitelist(project_root: str, language: str) -> set[str]:
    """Every capitalized token in the project's source files for `language`.

    Deliberately permissive, same rationale as grounding.project_whitelist:
    the committed tree compiles, so anything in it is a real identifier (or a
    comment word — harmless surplus). If a name appears NOWHERE in the repo,
    the model cannot legitimately be referencing it.
    """
    key = (project_root, language)
    cached = _project_cache.get(key)
    if cached and time.time() - cached[0] < _PROJECT_CACHE_TTL:
        return cached[1]

    globs = LANGUAGE_SPECS.get(language, {}).get("globs", [])
    tokens: set[str] = set()
    for pattern in globs:
        try:
            for p in Path(project_root).rglob(pattern):
                if any(s in p.parts for s in _SKIP_DIRS):
                    continue
                try:
                    src = p.read_text(errors="ignore")
                except OSError:
                    continue
                tokens |= set(_CAP_TOKEN.findall(src))
                # lowerCamelCase identifiers too — required for Dart/TS/Swift,
                # harmless for Go. Humped names only: plain lowercase words
                # would admit every English word in every comment.
                tokens |= set(_LOWER_CAMEL.findall(src))
        except Exception:
            continue

    _project_cache[key] = (time.time(), tokens)
    return tokens


def invalidate_cache(project_root: str | None = None) -> None:
    """Drop cached whitelists. Call after the agent writes new source files."""
    if project_root is None:
        _project_cache.clear()
        return
    for key in [k for k in _project_cache if k[0] == project_root]:
        del _project_cache[key]


def _stdlib_identifiers(language: str) -> set[str]:
    """Language stdlib identifiers. Only Go has a real scanner today."""
    if language != "go":
        return set()
    try:
        import grounding
        return grounding.stdlib_whitelist()[1]
    except Exception:
        return set()


# ── Checks ───────────────────────────────────────────────────────────────────

def _in_prohibition_context(text: str, ident: str) -> bool:
    """True if `ident` is named only in order to forbid it."""
    for m in re.finditer(r"\b" + re.escape(ident) + r"\b", text):
        # rstrip the qualifier dot: for "never use Galaxy.LegacyStep" the
        # window ends "...Galaxy." and the in-window lookahead cannot see the
        # identifier that follows it.
        window = text[max(0, m.start() - _PROHIBITION_WINDOW):m.start()].rstrip(".")
        if not _PROHIBITION.search(window):
            return False   # at least one non-prohibiting use → judge it
    return True


def _check_grounding(text: str, project_root: str, language: str,
                     verdict: Verdict, check_code_blocks: bool = False) -> list[str]:
    """Return identifiers referenced by `text` that exist nowhere legitimate."""
    spec = LANGUAGE_SPECS.get(language)
    if not spec or not spec["globs"]:
        verdict.skipped["grounding"] = f"no source globs for language '{language}'"
        return []

    try:
        proj = project_whitelist(project_root, language)
    except Exception as exc:
        verdict.skipped["grounding"] = f"whitelist build failed: {exc}"
        return []

    if len(proj) < MIN_WHITELIST_TOKENS:
        # Cannot tell "does not exist" from "we could not read the project".
        # Declining to judge is correct here — see module docstring.
        verdict.skipped["grounding"] = (
            f"project whitelist too small ({len(proj)} tokens < "
            f"{MIN_WHITELIST_TOKENS}) — greenfield or unreadable tree"
        )
        return []

    known = proj | spec["frameworks"] | UNIVERSAL_ALLOWLIST
    known |= _stdlib_identifiers(language)

    scanned = text if check_code_blocks else _CODE_FENCE.sub(" ", text)

    candidates = (set(_DOT_SELECTOR.findall(scanned))
                  | set(_MULTI_HUMP.findall(scanned))
                  | set(_LOWER_CAMEL_CALL.findall(scanned))
                  | set(_LOWER_CAMEL_SELECTOR.findall(scanned)))
    verdict.checked.append("grounding")
    return sorted(c for c in candidates
                  if c not in known and not _in_prohibition_context(scanned, c))


def _check_foreign_vocab(text: str, language: str, verdict: Verdict) -> list[str]:
    """Return names of other languages whose dialect this text speaks.

    Generalises promote_rules._FOREIGN_LANGUAGE_MARKERS, which only ever
    checked one direction (Go project rejecting Dart vocabulary). A Dart
    project should equally reject Go rules, and a Next.js project both.
    """
    if language == "generic":
        verdict.skipped["foreign_vocab"] = "project language unknown"
        return []

    verdict.checked.append("foreign_vocab")
    own = LANGUAGE_SPECS.get(language, {}).get("vocab", [])
    foreign: list[str] = []
    for other, spec in LANGUAGE_SPECS.items():
        if other in (language, "generic"):
            continue
        for pattern in spec["vocab"]:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    # Ambiguous token that both stacks use? Not foreign.
                    if any(re.search(o, text, re.IGNORECASE) for o in own):
                        continue
                    foreign.append(other)
                    break
            except re.error:
                continue
    return sorted(set(foreign))


# ── Public API ───────────────────────────────────────────────────────────────

def verify_prompt_artifact(text: str, project_root: str, *,
                           language: str | None = None,
                           kind: str = "rule",
                           mode: str = "reject",
                           check_code_blocks: bool = False) -> Verdict:
    """Verify model-generated text before it enters the prompt path.

    text         — the artifact (a rule, a hint, a drafted .roorules file)
    project_root — used to build the identifier whitelist
    language     — override; defaults to .sovereign_config.json "language"
    kind         — label used in messages ("rule", "hint", "roorules draft")
    mode         — "reject": failures set ok=False
                   "warn":   failures are recorded as warnings, ok stays True
                             (use where a human reviews the output, or where
                             the project has no source to ground against yet)
    check_code_blocks
                 — scan inside ``` fences too. Off by default because fenced
                   blocks in a rule are illustrations of what NOT to write.

    Never raises. On internal error, returns ok=True with the error recorded
    as a warning: a broken verifier must not become a broken pipeline.
    """
    verdict = Verdict()
    if not text or not text.strip():
        return verdict

    try:
        lang = normalise_language(language) if language else project_language(project_root)

        ungrounded = _check_grounding(text, project_root, lang, verdict,
                                      check_code_blocks=check_code_blocks)
        foreign = _check_foreign_vocab(text, lang, verdict)

        if foreign:
            msg = (f"{kind} speaks {'/'.join(foreign)} vocabulary "
                   f"on a {lang} project")
            (verdict.reasons if mode == "reject" else verdict.warnings).append(msg)

        if ungrounded:
            shown = ", ".join(ungrounded[:6])
            more = f" (+{len(ungrounded) - 6} more)" if len(ungrounded) > 6 else ""
            msg = (f"{kind} references identifiers not found in project, "
                   f"stdlib, or framework: {shown}{more}")
            (verdict.reasons if mode == "reject" else verdict.warnings).append(msg)

        verdict.ok = not verdict.reasons

    except Exception as exc:  # pragma: no cover — defensive by design
        verdict.warnings.append(f"verifier error ({exc}) — artifact allowed through")
        verdict.ok = True

    return verdict


def verify_many(texts: list[str], project_root: str, **kwargs) -> list[tuple[str, Verdict]]:
    """Verify a batch, returning (text, verdict) pairs in input order."""
    return [(t, verify_prompt_artifact(t, project_root, **kwargs)) for t in texts]


def partition(texts: list[str], project_root: str, **kwargs) -> tuple[list[str], list[tuple[str, Verdict]]]:
    """Split a batch into (accepted, [(rejected, verdict), ...])."""
    accepted: list[str] = []
    rejected: list[tuple[str, Verdict]] = []
    for text, verdict in verify_many(texts, project_root, **kwargs):
        (accepted.append(text) if verdict.ok else rejected.append((text, verdict)))
    return accepted, rejected
