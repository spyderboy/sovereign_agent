"""
Initialises a project folder for use with sovereign_agent.

Generates the three core context files the automation needs:
  VISION.md    — what the project is, why it exists, definition of done
  .roorules    — coding conventions injected into every model prompt
  CHANGELOG.md — running change log (updated by the agent)
  sovereign.json — registration record

Usage:
    python init_project.py /path/to/project
    python init_project.py /path/to/project --name "My App" --stack nextjs
    python init_project.py /path/to/project --stack swift --existing

Supported stacks:
    flutter   — Flutter/Dart (default)
    nextjs    — Next.js / TypeScript / React
    swift     — Swift / SwiftUI / iOS
    python    — Python service or CLI
    generic   — Language-agnostic baseline

Flags:
    --existing   Initialise into an existing codebase (adjusts template tone)
    --name       Display name used in VISION.md header
    --stack      Which stack template to use (default: generic)
"""

import os
import sys
import json
import argparse
from datetime import date

# ─── VISION templates ─────────────────────────────────────────────────────────

VISION_GENERIC = """\
# Project Vision — {name}

## Philosophy
[One sentence: the guiding principle behind every decision]

## What we're building
[One paragraph: what the product does and who it's for]

## What is in scope
- [Feature area 1]
- [Feature area 2]
- [Feature area 3]

## What is OUT of scope — do not touch
- [Boundary 1 — e.g. existing backend APIs]
- [Boundary 2]

## Tech stack
- Language:  [language + version]
- Framework: [framework]
- State/data: [state management or ORM]
- Hosting:   [where it runs]

## Definition of done for v1.0
[One sentence: what does "shipped" mean for this project?]
"""

VISION_FLUTTER = """\
# Project Vision — {name}

## Philosophy
Resolution over perfection. Ship small, working slices and iterate.

## What we're building
[One paragraph: what the app does and who it's for]

## What this project is
{existing_note}

## What is in scope
- All screens, navigation, and UI components
- [API layer — list key integrations]
- Local state management
- Platform builds (iOS primary, Android secondary)

## What is OUT of scope — do not touch
- [Backend / Cloud Functions — if applicable]
- API contracts and endpoint URLs — consume them as-is
- [Existing data models — if applicable]

## Tech stack
- Flutter / Dart
- State:  [Riverpod / Bloc / Provider]
- API:    [http / dio / chopper]
- Auth:   [Firebase Auth / custom]

## Definition of done for v1.0
[One sentence describing the playable/shippable milestone]
"""

VISION_NEXTJS = """\
# Project Vision — {name}

## Philosophy
[One sentence guiding principle]

## What we're building
[One paragraph: what the product does and who it's for]

## What this project is
{existing_note}

## What is in scope
- All pages, layouts, and UI components
- API routes and server actions
- Authentication and session management
- [Data layer — database / CMS / external APIs]

## What is OUT of scope
- [Existing backend services — if applicable]
- [External data contracts — consume as-is]

## Tech stack
- Next.js (App Router)
- TypeScript — strict mode on
- Styling:  [Tailwind CSS / CSS Modules / styled-components]
- Database: [Prisma + PostgreSQL / Supabase / PlanetScale / none]
- Auth:     [NextAuth.js / Clerk / custom]
- Hosting:  [Vercel / AWS / self-hosted]

## Definition of done for v1.0
[One sentence: what does "shipped" look like?]
"""

VISION_SWIFT = """\
# Project Vision — {name}

## Philosophy
[One sentence guiding principle]

## What we're building
[One paragraph: what the iOS/macOS app does and who it's for]

## What this project is
{existing_note}

## What is in scope
- All views, navigation, and UI
- [Networking / API integration]
- Local persistence
- App Store submission

## What is OUT of scope
- [Existing backend — if applicable]
- [Shared frameworks or SPM packages not owned by this project]

## Tech stack
- Swift / SwiftUI
- Minimum deployment: [iOS 17 / macOS 14]
- State:   [@Observable / @StateObject / TCA]
- Network: [URLSession / Alamofire / custom]
- DB:      [SwiftData / CoreData / UserDefaults]

## Definition of done for v1.0
[One sentence: what does App Store approval look like?]
"""

VISION_PYTHON = """\
# Project Vision — {name}

## Philosophy
[One sentence guiding principle]

## What we're building
[One paragraph: what the service/tool does and who it's for]

## What this project is
{existing_note}

## What is in scope
- [Core feature area 1]
- [Core feature area 2]
- [CLI / API / scheduled job — pick what applies]

## What is OUT of scope
- [External systems — consume their APIs as-is]

## Tech stack
- Python 3.12+
- Web/API:   [FastAPI / Flask / none]
- DB:        [SQLAlchemy / SQLite / Postgres]
- Task queue:[Celery / RQ / none]
- Hosting:   [GCP Cloud Run / AWS Lambda / bare VM]

## Definition of done for v1.0
[One sentence: shipped, tested, deployed]
"""

# ─── .roorules templates ──────────────────────────────────────────────────────

ROORULES_GENERIC = """\
# Project rules — read and follow these on every task

## What this project is
[Fill in: one sentence on what this is and what it is not]

## Hard boundaries
- [Boundary 1 — e.g. "never modify files in /api — that layer is locked"]
- [Boundary 2]

## File size
- Keep every file under 200 lines. Split into focused modules if exceeded.

## Code style
- [Style conventions for your language]
- One class/component per file where the language supports it.
- Every async operation must handle loading and error states explicitly.

## Versioning & changelog
- Update CHANGELOG.md with every meaningful change (Keep a Changelog format).
- Commit message format: type(scope): description
  Types: feat | fix | chore | refactor | test | docs

## What NOT to do
- Do not add dependencies without checking maintenance status and licence.
- Do not commit secrets, API keys, or .env files.
- Do not invent interfaces that don't exist in the current codebase.
"""

ROORULES_FLUTTER = """\
# Project rules — read and follow these on every task

## What this project is
[Fill in: one sentence on what this Flutter app is and is not]

## Hard boundaries
- NEVER modify files in functions/ or backend/ — those are locked.
- NEVER change API endpoint URLs or response shapes.
- If a task requires a backend change, STOP and flag for human review.

## File size
- Keep every file under 150 lines. Split into smaller focused modules if exceeded.

## Project structure
```
lib/
  features/<feature>/
    data/         — repositories, API clients, DTOs
    domain/       — entities, use cases, interfaces
    presentation/ — widgets, screens, providers
```

## Flutter conventions
- Prefer const constructors wherever possible.
- One widget per file. Widget files live in presentation/.
- Use named routes (GoRouter preferred). No anonymous MaterialPageRoute push.
- Every async operation must have a loading state and an error state.
- State exposed to UI via Riverpod providers only.

## pubspec.yaml is LOCKED
Do not add packages without explicit approval. Check pub.dev score and last-updated
date before proposing any new dependency.

## Versioning & changelog
- Update CHANGELOG.md using Keep a Changelog format.
- Bump pubspec.yaml version at milestones: patch / minor / major.
- Commit format: type(scope): description

## LOCKED FILES — do NOT rewrite these
[List stable files here as the project matures]

## What NOT to do
- Do not commit secrets or .env files.
- Do not use global mutable state outside of Riverpod providers.
- Do not invent API methods or model fields that are not documented.
"""

ROORULES_NEXTJS = """\
# Project rules — read and follow these on every task

## What this project is
[Fill in: one sentence on what this Next.js app is and is not]

## Hard boundaries
- NEVER modify files outside of src/ unless explicitly instructed.
- NEVER change API route contracts or database schemas without a migration.
- Environment variables live in .env.local — never hardcode them.

## TypeScript
- Strict mode is ON. Zero `any` types. No `as unknown as X` casts.
- Prefer `interface` over `type` for object shapes.
- All server actions and API route handlers must be explicitly typed.

## App Router conventions
- Pages live in app/. No pages/ directory (App Router only).
- Layouts inherit from the nearest layout.tsx up the tree.
- Use React Server Components by default; add "use client" only when needed.
- Data fetching happens in Server Components or Server Actions — not useEffect.
- Loading states: use loading.tsx files, not manual loading booleans.
- Error states: use error.tsx files, not try/catch in render.

## File structure
```
src/
  app/               — pages, layouts, API routes (App Router)
  components/        — shared UI components
  lib/               — utilities, constants, type definitions
  server/            — server-only code (DB queries, auth helpers)
```
- One component per file.
- Co-locate tests next to the file they test (foo.test.ts beside foo.ts).

## Styling
- Use Tailwind utility classes only. No inline styles. No CSS-in-JS.
- Responsive breakpoints: mobile-first (sm: md: lg:).
- Dark mode: use dark: prefix, never hardcode colours.

## Dependencies
- Do not add npm packages without checking weekly downloads and last publish date.
- Prefer built-in Next.js features (Image, Link, Font) over third-party equivalents.

## Versioning & changelog
- Update CHANGELOG.md using Keep a Changelog format.
- Commit format: type(scope): description

## LOCKED FILES — do NOT rewrite these
[List stable files here as the project matures]

## What NOT to do
- Do not use getServerSideProps or getStaticProps (App Router, not Pages Router).
- Do not fetch data in useEffect — use Server Components or React Query.
- Do not commit .env.local or any file containing secrets.
- Do not add `console.log` statements to production code paths.
"""

ROORULES_SWIFT = """\
# Project rules — read and follow these on every task

## What this project is
[Fill in: one sentence on what this Swift app is and is not]

## Hard boundaries
- NEVER modify the backend or any shared framework this app depends on.
- NEVER change API contracts or data model schemas without a migration plan.

## Swift conventions
- Swift 5.9+. Use structured concurrency (async/await, actors) — no DispatchQueue.
- Prefer value types (struct, enum) over classes. Use classes only for reference semantics.
- Use @Observable (iOS 17+) or @StateObject for view models. No raw ObservableObject.
- Mark all network and disk I/O as async throws. Never block the main actor.

## SwiftUI conventions
- One View per file. File name matches the View name.
- Views are dumb: no business logic, no network calls, no formatting.
- All formatting (dates, numbers, currency) happens in the ViewModel or via formatters.
- NavigationStack only — no deprecated NavigationView.

## File structure
```
Sources/
  Features/<Feature>/
    <Feature>View.swift       — SwiftUI view
    <Feature>ViewModel.swift  — @Observable view model
  Models/                     — Plain data types (Codable structs)
  Services/                   — Network, persistence, auth
  Utilities/                  — Extensions, helpers
```

## Error handling
- All thrown errors must be caught and surfaced to the user — never silently swallowed.
- Use a typed Error enum per domain, not NSError or generic Swift.Error where avoidable.

## Testing
- Use XCTest. Unit-test ViewModels and Services; don't unit-test Views.
- Mock network calls with URLProtocol stubs, not Cuckoo or Mockingbird.

## Versioning & changelog
- Update CHANGELOG.md using Keep a Changelog format.
- Commit format: type(scope): description

## LOCKED FILES — do NOT rewrite these
[List stable files here as the project matures]

## What NOT to do
- Do not use UIKit in SwiftUI files — keep the boundary clean.
- Do not force-unwrap optionals. Use guard let, if let, or provide defaults.
- Do not add Swift packages without checking maintenance status.
- Do not commit API keys or secrets — use Xcode's xcconfig / environment vars.
"""

ROORULES_PYTHON = """\
# Project rules — read and follow these on every task

## What this project is
[Fill in: one sentence on what this Python project is and is not]

## Hard boundaries
- NEVER modify files outside src/ or the explicitly listed entry points.
- NEVER change public API signatures without updating all callers in the same task.

## Code style
- Python 3.12+. Type hints on every function signature — no bare `Any`.
- Formatted with Black (line length 88). Sorted imports with isort.
- f-strings for formatting — no % formatting or .format() calls.
- Prefer dataclasses or Pydantic models over plain dicts for structured data.

## Project structure
```
src/
  <package_name>/
    api/      — HTTP handlers or CLI entry points
    domain/   — business logic, pure functions, no I/O
    infra/    — database, external services, file I/O
    models/   — Pydantic models / dataclasses
tests/        — mirrors src/ structure
```
- No business logic in api/ or infra/ layers — push it to domain/.
- No I/O in domain/ — pure functions only.

## Dependencies
- Everything in requirements.txt or pyproject.toml. No ad-hoc pip installs.
- Prefer stdlib over third-party where the stdlib version is adequate.

## Testing
- pytest only. Aim for 80%+ coverage on domain/ and models/.
- Use pytest fixtures for shared setup. No module-level test state.
- Mock I/O with pytest-mock or unittest.mock — never call real external services.

## Errors
- Raise specific exceptions, not bare Exception.
- Log with structlog or logging — never print() in production paths.

## Versioning & changelog
- Update CHANGELOG.md using Keep a Changelog format.
- Commit format: type(scope): description

## LOCKED FILES — do NOT rewrite these
[List stable files here as the project matures]

## What NOT to do
- Do not use mutable default arguments (def foo(items=[])).
- Do not import from tests in production code.
- Do not commit .env files or hardcoded credentials.
- Do not add `print()` calls in non-CLI production paths — use logging.
"""

CHANGELOG_TEMPLATE = """\
# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

"""

# ─── Template dispatch ────────────────────────────────────────────────────────

VISION_TEMPLATES = {
    "flutter": VISION_FLUTTER,
    "nextjs":  VISION_NEXTJS,
    "swift":   VISION_SWIFT,
    "python":  VISION_PYTHON,
    "generic": VISION_GENERIC,
}

ROORULES_TEMPLATES = {
    "flutter": ROORULES_FLUTTER,
    "nextjs":  ROORULES_NEXTJS,
    "swift":   ROORULES_SWIFT,
    "python":  ROORULES_PYTHON,
    "generic": ROORULES_GENERIC,
}

EXISTING_NOTES = {
    "flutter": (
        "A Flutter frontend for an existing backend. The backend APIs, data models, "
        "and infrastructure are complete and must not be modified."
    ),
    "nextjs": (
        "A Next.js frontend for an existing backend or API layer. "
        "Consume existing endpoints as-is; do not change contracts."
    ),
    "swift": (
        "A SwiftUI app consuming an existing backend API. "
        "API contracts and backend services are locked."
    ),
    "python": (
        "A Python service augmenting an existing system. "
        "External service interfaces are locked — consume them as-is."
    ),
    "generic": (
        "An addition to an existing system. The existing interfaces and "
        "data contracts are locked — consume them as-is."
    ),
}

NEW_NOTES = {
    "flutter": "A greenfield Flutter app built from scratch.",
    "nextjs":  "A greenfield Next.js application built from scratch.",
    "swift":   "A greenfield SwiftUI app built from scratch.",
    "python":  "A greenfield Python project built from scratch.",
    "generic": "A greenfield project built from scratch.",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def write_if_missing(path: str, content: str, label: str):
    if os.path.exists(path):
        print(f"  ⚠  {label} already exists — skipping (delete to regenerate)")
    else:
        with open(path, "w") as f:
            f.write(content)
        print(f"  ✓  {label} created")


def main():
    parser = argparse.ArgumentParser(
        description="Initialise a project folder for sovereign_agent automation."
    )
    parser.add_argument("project", help="Path to the project folder")
    parser.add_argument("--name",  help="Display name (used in VISION.md)", default=None)
    parser.add_argument(
        "--stack",
        choices=["flutter", "nextjs", "swift", "python", "generic"],
        default="generic",
        help="Tech stack template to use (default: generic)",
    )
    parser.add_argument(
        "--existing",
        action="store_true",
        help="Initialise into an existing codebase (adjusts template tone)",
    )
    args = parser.parse_args()

    project_path = os.path.abspath(args.project)
    if not os.path.isdir(project_path):
        print(f"⚠  Directory not found: {project_path}")
        sys.exit(1)

    name = args.name or os.path.basename(project_path)
    stack = args.stack
    existing_note = EXISTING_NOTES[stack] if args.existing else NEW_NOTES[stack]

    print(f"\nInitialising sovereign_agent for: {project_path}")
    print(f"  Stack:    {stack}")
    print(f"  Mode:     {'existing project' if args.existing else 'new project'}\n")

    # Create standard directories
    for subdir in ["logs", "release-notes", "docs"]:
        os.makedirs(os.path.join(project_path, subdir), exist_ok=True)

    # Write context files
    write_if_missing(
        os.path.join(project_path, "VISION.md"),
        VISION_TEMPLATES[stack].format(name=name, existing_note=existing_note),
        "VISION.md",
    )
    write_if_missing(
        os.path.join(project_path, ".roorules"),
        ROORULES_TEMPLATES[stack],
        ".roorules",
    )
    write_if_missing(
        os.path.join(project_path, "CHANGELOG.md"),
        CHANGELOG_TEMPLATE,
        "CHANGELOG.md",
    )

    # Registration record
    sovereign_path = os.path.join(project_path, "sovereign.json")
    if not os.path.exists(sovereign_path):
        config = {
            "name":       name,
            "stack":      stack,
            "existing":   args.existing,
            "path":       project_path,
            "created":    date.today().isoformat(),
            "agent_home": os.path.dirname(os.path.abspath(__file__)),
        }
        with open(sovereign_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"  ✓  sovereign.json created")

    print(f"""
Next steps:
  1. Edit VISION.md        — fill in what you're building and any hard boundaries
  2. Edit .roorules        — add API signatures, locked files, and stack-specific traps
  3. If new project:       scaffold the codebase, then run plan_week.py to generate tasks
  4. If existing project:  run plan_week.py to generate a ROADMAP from current state

  python plan_week.py --project {project_path}

See MANUAL.md → "Project Onboarding" for the full ideation → automation workflow.
""")


if __name__ == "__main__":
    main()
