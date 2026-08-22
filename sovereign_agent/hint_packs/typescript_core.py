"""
typescript_core — TypeScript/Next.js language traps. Applies to ANY
TypeScript project.

Seeded 2026-08-22 by graduating the genuinely language-general entries out
of projectsdash's .sovereign_config.json additional_bad_patterns /
additional_error_hints (that file's business-logic-specific entries —
projectsdash's ops-execution rules, its no-git-write rule, its no-auth
rule, etc. — correctly stay there; see hint_packs/README.md's "narrowest
pack" rule). This mirrors exactly how dart_core.py itself was created: real
traps observed on a real project, promoted once they were confirmed general
rather than project-specific.

Every project on this harness before 2026-08-22 was Dart, so — same as
qwen_advisor.py's persona and work.py's coding-agent role line — this
directory had a single BAD_PATTERNS entry flagging the OPPOSITE mistake
(a model reaching for Dart/Flutter/Riverpod APIs on a non-Dart project) only
because one project's config happened to hand-add it. Split into its own
pack here so every TypeScript project gets it by default, the same way
dart_core's Dart traps load by default for every Dart project.
"""

BAD_PATTERNS: list[tuple[str, str]] = [
    (r'\.withOpacity\(',
     "That's a Flutter/Dart API — this is a TypeScript project. If you're "
     "reaching for Flutter APIs you are pattern-matching on the wrong stack; "
     "there is no Flutter anywhere in this codebase."),
    (r'StateNotifier|riverpod|flutter_riverpod',
     "No Riverpod, no Flutter state management here — this is TypeScript/"
     "React. Component state is useState/useReducer; server state is "
     "fetched via API routes."),
    (r'\{\s*params\s*\}\s*:\s*\{\s*params\s*:\s*\{',
     "NEXT.JS ASYNC PARAMS: on Next.js 15+, dynamic route `params` (and page "
     "`params` props) are ALWAYS `Promise<{...}>`, never a plain object. Use "
     "`{ params }: { params: Promise<{ id: string }> }` and "
     "`const { id } = await params`. There is no synchronous-params mode in "
     "15+. If this project is on an older Next.js version, check its "
     ".roorules before assuming this applies."),
    (r'next/image',
     "Using next/image assumes the project's images go through Next's "
     "remote-optimization pipeline. Check the project's .roorules/config "
     "before assuming that's true here — several projects on this harness "
     "serve only local files via a plain <img> tag and deliberately don't "
     "use next/image."),
]

ERROR_HINTS: list[tuple[str, str]] = [
    (r'error TS25(32|45|22):.*undefined',
     "If this project has noUncheckedIndexedAccess enabled in tsconfig.json "
     "(check tsconfig.json before assuming), every array index (arr[0]), "
     "object/record lookup (obj[key]), and regex match result "
     "(str.match(re)[1]) is typed T | undefined, never T, even when you "
     "know it exists at runtime — TypeScript does not narrow this "
     "automatically. This shows up as three different error codes depending "
     "on where the value is used: TS2532 ('Object is possibly undefined') "
     "on direct property access, TS2345 ('Argument of type ... undefined "
     "... is not assignable') when passing it to a function, TS2322 "
     "('Type ... undefined ... is not assignable') when assigning it — all "
     "three are the same root cause. Guard explicitly before using the "
     "value: an if-check, a destructure with a fallback, or (only when "
     "truly guaranteed) a non-null assertion — never assume indexing "
     "already narrowed the type."),
]
