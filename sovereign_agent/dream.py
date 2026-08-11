"""
dream.py — Project ideation and bootstrap assistant.

Turns a description into filled-in VISION.md, .roorules, and an initial
ROADMAP.md. Files are drafted by a local LLM (or Claude API), shown for
your approval, and only written after you accept them.

Usage:
    python dream.py --project ~/Code/my_app
    python dream.py --project ~/Code/my_app --stack nextjs
    python dream.py --project ~/Code/my_app --idea "A habit tracker iOS app"
    python dream.py --project ~/Code/my_app --stack swift --existing
    python dream.py --project ~/Code/my_app --claude   # use Claude API

Supported stacks: flutter | nextjs | swift | python | generic

How it works:
  1. You describe the project in one sentence (or pass --idea)
  2. The script scans the existing folder if it already has code
  3. A large local model (or Claude) drafts VISION.md, .roorules, ROADMAP.md
  4. You review each draft: accept, regenerate with feedback, or skip
  5. Accepted files are written and the project is wired up (sovereign.json)
  6. Run the supervisor to start autonomous coding

Environment variables (in sovereign_agent/.env):
  DREAM_MODEL       — Ollama model to use for drafting (default: TIER4_MODEL)
  ANTHROPIC_API_KEY — Required only when --claude is passed
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

import requests

import prompt_artifacts  # grounding gate for model-written prompt text

# ── Config ─────────────────────────────────────────────────────────────────────

OLLAMA_URL  = os.getenv("LOCAL_MODEL_URL", "http://localhost:11434")
DREAM_MODEL = os.getenv("DREAM_MODEL",
              os.getenv("TIER4_MODEL",
              os.getenv("TIER3_MODEL", "qwen2.5-coder:32b")))

BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
RESET  = "\033[0m"

STACK_LABELS = {
    "flutter": "Flutter / Dart (mobile)",
    "nextjs":  "Next.js / TypeScript (web)",
    "swift":   "Swift / SwiftUI (iOS/macOS)",
    "python":  "Python (service / CLI)",
    "generic": "Generic / Other",
}

VALIDATION_CMDS = {
    "flutter": "`flutter analyze`",
    "nextjs":  "`npm test`",
    "swift":   "`swift build` / `xcodebuild test`",
    "python":  "`pytest`",
    "generic": "(set in .roorules)",
}


# ── LLM callers ────────────────────────────────────────────────────────────────

def _call_ollama(prompt: str, max_tokens: int = 2000) -> str:
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": DREAM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": max_tokens},
            },
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except Exception as e:
        print(f"\n{RED}Ollama error: {e}{RESET}")
        print(f"  Is Ollama running? Try: ollama serve")
        sys.exit(1)


def _call_claude(prompt: str, max_tokens: int = 2000) -> str:
    try:
        import anthropic
    except ImportError:
        print(f"\n{YELLOW}anthropic library not installed.")
        print(f"  Run: pip install anthropic --break-system-packages")
        print(f"  Falling back to Ollama ({DREAM_MODEL}).{RESET}")
        return _call_ollama(prompt, max_tokens)
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        print(f"\n{YELLOW}Claude API error: {e} — falling back to Ollama{RESET}")
        return _call_ollama(prompt, max_tokens)


def call_llm(prompt: str, use_claude: bool = False, max_tokens: int = 2000) -> str:
    if use_claude and os.getenv("ANTHROPIC_API_KEY"):
        print(f"  {DIM}Calling Claude API...{RESET}", flush=True)
        return _call_claude(prompt, max_tokens)
    print(f"  {DIM}Calling Ollama ({DREAM_MODEL})...{RESET}", flush=True)
    return _call_ollama(prompt, max_tokens)


# ── Project scanner ────────────────────────────────────────────────────────────

def scan_project(project_path: str) -> str:
    """Return a concise structural snapshot of an existing project."""
    p = Path(project_path)
    lines = []

    # Top-level listing (skip hidden files)
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    top = [e.name for e in entries if not e.name.startswith(".")][:24]
    lines.append("Top-level: " + ", ".join(top))

    # Key manifest / config files
    for fname in ["README.md", "package.json", "pubspec.yaml",
                  "pyproject.toml", "Package.swift", "go.mod", "Cargo.toml"]:
        fpath = p / fname
        if fpath.exists():
            try:
                content = fpath.read_text(errors="replace")[:600]
                lines.append(f"\n--- {fname} ---\n{content}")
            except Exception:
                pass

    # Source directory shallow listing
    for srcdir in ["src", "lib", "app", "Sources", "cmd", "pages"]:
        sdir = p / srcdir
        if sdir.exists() and sdir.is_dir():
            subdirs = [x.name for x in sorted(sdir.iterdir())[:14]]
            lines.append(f"\n{srcdir}/: " + ", ".join(subdirs))

    return "\n".join(lines)[:3000]


# ── LLM prompts ────────────────────────────────────────────────────────────────

def draft_vision(name: str, stack: str, idea: str, existing: bool,
                 scan: str, use_claude: bool, feedback: str = "") -> str:
    mode = "existing codebase" if existing else "new project"
    scan_block = f"\n\nExisting project scan:\n{scan}" if scan else ""
    revision = f"\n\nRevision request: {feedback}" if feedback else ""
    prompt = f"""You are a product architect. Write a complete, specific VISION.md for a software project.

Project name: {name}
Stack: {STACK_LABELS[stack]}
Mode: {mode}
Description: {idea}{revision}{scan_block}

Fill in ALL sections with real, specific content — no placeholder text in square brackets.
Use exactly this structure (no more, no less):

# Project Vision — {name}

## Philosophy
(One guiding sentence about the product mindset)

## What we're building
(One paragraph: concrete description of the product and its users)

## What is in scope
- (3–5 concrete feature areas — be specific about screens, endpoints, or modules)

## What is OUT of scope — do not touch
- (2–4 hard boundaries; be specific about what's locked or excluded)

## Tech stack
- Language/framework + version
- State management or ORM
- Testing approach
- Hosting/deployment target

## Definition of done for v1.0
(One sentence: what does "shipped" mean for this project?)

Output ONLY the markdown content. No explanation before or after."""
    return call_llm(prompt, use_claude, max_tokens=1600)


def draft_roorules(name: str, stack: str, vision: str,
                   use_claude: bool, feedback: str = "") -> str:
    revision = f"\n\nRevision request: {feedback}" if feedback else ""
    prompt = f"""You are a senior engineer setting up strict coding rules for an AI coding agent.
Write a complete .roorules file for this project.{revision}

Project: {name}
Stack: {STACK_LABELS[stack]}
Validation command: {VALIDATION_CMDS[stack]}

Vision summary:
{vision[:1200]}

The .roorules file is injected verbatim into every AI coding prompt — make it precise and strict.
Rules for the file:
- Name real paths and APIs from the Vision above, not generic placeholders
- The agent must never invent APIs, types, or methods not in the codebase
- Include hard boundaries about what files/layers are locked
- One component/file per class rule where applicable

Use exactly this structure:

# Project rules — {name}

## What this project is
(One sentence — match the vision)

## Hard boundaries
- (2–4 strict lines; name real paths if known)

## File size
- Keep every file under N lines. Split into focused modules if exceeded.

## Project structure
```
(Realistic directory tree for {STACK_LABELS[stack]})
```

## {STACK_LABELS[stack].split('/')[0].strip()} conventions
- (6–10 specific rules for this stack)

## Validation
- Run {VALIDATION_CMDS[stack]} before marking any task done.
- Fix all errors before writing the next file.

## Versioning & changelog
- Update CHANGELOG.md using Keep a Changelog format.
- Commit format: type(scope): description
  Types: feat | fix | chore | refactor | test | docs

## LOCKED FILES — do NOT rewrite these
[List stable files here as the project matures]

## What NOT to do
- (5–7 specific anti-patterns for this stack and project)

Output ONLY the file content. No explanation."""
    return call_llm(prompt, use_claude, max_tokens=1800)


def draft_roadmap(name: str, stack: str, vision: str, existing: bool,
                  use_claude: bool, feedback: str = "") -> str:
    task_count = "15–20" if existing else "22–30"
    framing = (
        "This is an EXISTING project. Begin with audit, cleanup, and refactor tasks "
        "before adding features. The agent needs a safe foundation to build on."
        if existing else
        "This is a NEW project. Start with scaffolding and core data models, "
        "then features, then tests and polish."
    )
    revision = f"\n\nRevision request: {feedback}" if feedback else ""
    prompt = f"""You are a software engineering lead planning a sprint for an AI coding agent.
The agent will execute these tasks autonomously — each must be precise and self-contained.{revision}

Project: {name}
Stack: {STACK_LABELS[stack]}
{framing}

Vision:
{vision[:1400]}

Write a ROADMAP.md with {task_count} tasks. Rules:
- Each task is EXACTLY one line: `- [ ] <imperative description>`
- No sub-bullets, no explanations under tasks
- Dependency order is critical — earlier tasks must not depend on later ones
- Each task touches 1–3 files at most — not broad sweeps
- Name specific files, components, or screens wherever possible
- Group into sprints with `## Sprint N — <Theme>` headers
- Include at minimum: core data models, 3+ main screens/routes/endpoints, key services, 2 test tasks

Good task: `- [ ] Add UserProfile model (name, avatar, bio) to lib/models/user_profile.dart`
Bad task:  `- [ ] Set up the project` (too vague)
Bad task:  `- [ ] Implement all authentication screens` (too broad)

Output:
# ROADMAP — {name}

## Sprint 1 — {"Cleanup & Models" if existing else "Scaffolding & Models"}
- [ ] ...

## Sprint 2 — Core Features
- [ ] ...

## Sprint 3 — Polish & Tests
- [ ] ...

Output ONLY the markdown. No explanation."""
    return call_llm(prompt, use_claude, max_tokens=2800)


# ── Interactive review ─────────────────────────────────────────────────────────

W = 62

def _show_draft(label: str, content: str):
    print(f"\n{'─'*W}")
    print(f"{BOLD}  Draft {label}{RESET}")
    print(f"{'─'*W}")
    lines = content.splitlines()
    for line in lines[:45]:
        print(f"  {line}")
    if len(lines) > 45:
        print(f"  {DIM}... [{len(lines) - 45} more lines]{RESET}")
    print(f"{'─'*W}")


def review(label: str, content: str) -> tuple[str | None, str | None]:
    """
    Show a draft and ask the user to accept, regenerate, or skip.

    Returns:
        (accepted_content, None)   — user accepted
        (None, feedback)           — user wants a revision; feedback may be empty string
        (None, None)               — user skipped (use last draft as-is)
    """
    _show_draft(label, content)
    print(
        f"\n  {GREEN}[a]{RESET}ccept  "
        f"{YELLOW}[r]{RESET}egenerate with feedback  "
        f"{DIM}[s]{RESET}kip  "
        f"{DIM}[q]{RESET}uit",
        end=" → ",
    )
    try:
        choice = input().strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)

    if choice in ("a", ""):
        return content, None
    if choice == "r":
        print("  What to change? (Enter = same prompt, try again): ", end="")
        try:
            fb = input().strip()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(130)
        return None, fb or "(regenerate)"
    if choice == "s":
        return None, None
    if choice == "q":
        print(f"\n  {DIM}Exiting without writing files.{RESET}")
        sys.exit(0)
    return content, None  # default accept on unknown input


def write_file(path: str, content: str, label: str):
    if os.path.exists(path):
        print(f"  {YELLOW}{label} already exists — overwrite? [y/N]{RESET}", end=" ")
        try:
            ans = input().strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if ans != "y":
            print(f"  {DIM}Skipped.{RESET}")
            return
    with open(path, "w") as f:
        f.write(content)
    print(f"  {GREEN}✓  {label}{RESET}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Draft VISION.md, .roorules, and ROADMAP.md for sovereign_agent."
    )
    parser.add_argument("--project", "-p", required=True,
                        help="Path to the project folder")
    parser.add_argument("--name",
                        help="Display name (defaults to folder name)")
    parser.add_argument("--stack",
                        choices=["flutter", "nextjs", "swift", "python", "generic"],
                        default="generic",
                        help="Tech stack template (default: generic)")
    parser.add_argument("--idea",
                        help="One-sentence description of what you're building")
    parser.add_argument("--existing", action="store_true",
                        help="Initialising into an existing codebase")
    parser.add_argument("--claude", action="store_true",
                        help="Use Claude API instead of Ollama (requires ANTHROPIC_API_KEY)")
    parser.add_argument("--no-roadmap", action="store_true",
                        help="Skip ROADMAP.md generation (generate it later with plan_week.py)")
    args = parser.parse_args()

    project_path = os.path.abspath(os.path.expanduser(args.project))
    os.makedirs(project_path, exist_ok=True)

    name = args.name or Path(project_path).name
    stack = args.stack
    use_claude = args.claude

    if use_claude and not os.getenv("ANTHROPIC_API_KEY"):
        print(f"\n{YELLOW}--claude passed but ANTHROPIC_API_KEY is not set.")
        print(f"  Add it to sovereign_agent/.env or export it in your shell.")
        print(f"  Falling back to Ollama.{RESET}")
        use_claude = False

    # ── Header ────────────────────────────────────────────────────────────────
    backend = "Claude API" if use_claude else f"Ollama ({DREAM_MODEL})"
    print(f"\n{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}  🌱  dream.py — sovereign_agent bootstrap{RESET}")
    print(f"{BOLD}  Project : {name}{RESET}")
    print(f"{BOLD}  Stack   : {STACK_LABELS[stack]}{RESET}")
    print(f"{BOLD}  Model   : {backend}{RESET}")
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}\n")

    # ── Get project description ───────────────────────────────────────────────
    idea = args.idea
    if not idea:
        print(f"  Describe what you're building (1–3 sentences):")
        print(f"  ", end="", flush=True)
        try:
            idea = input().strip()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(0)
        if not idea:
            idea = f"A {STACK_LABELS[stack]} application."
            print(f"  {DIM}Using default description: {idea}{RESET}")

    # ── Scan project if it already has content ────────────────────────────────
    scan = ""
    p = Path(project_path)
    non_hidden = [x for x in p.iterdir() if not x.name.startswith(".")]
    if non_hidden:
        print(f"\n  Scanning existing project...")
        scan = scan_project(project_path)
        print(f"  {DIM}{scan.splitlines()[0]}{RESET}")

    print()

    # ── Step 1: VISION.md ─────────────────────────────────────────────────────
    print(f"  {BOLD}Step 1/3 — VISION.md{RESET}")
    vision_content = None
    feedback = ""
    while vision_content is None:
        draft = draft_vision(name, stack, idea, args.existing, scan, use_claude, feedback)
        accepted, feedback = review("VISION.md", draft)
        if accepted is not None:
            vision_content = accepted
        elif feedback is None:
            # Skipped — use last draft
            vision_content = draft
            print(f"  {DIM}VISION.md skipped — using last draft as-is.{RESET}")
        # else: feedback is set → loop and regenerate

    # ── Step 2: .roorules ─────────────────────────────────────────────────────
    print(f"\n  {BOLD}Step 2/3 — .roorules{RESET}")
    rules_content = None
    feedback = ""
    while rules_content is None:
        draft = draft_roorules(name, stack, vision_content, use_claude, feedback)
        # .roorules is injected verbatim into every future prompt, so a
        # hallucinated identifier here is permanent priming — the widest blast
        # radius of any model-written artifact in the system. Warn rather than
        # reject: on a greenfield tree there is no source to ground against,
        # and a human is reviewing the draft on the next line anyway.
        _v = prompt_artifacts.verify_prompt_artifact(
            draft, project_path, language=stack,
            kind=".roorules draft", mode="warn",
        )
        for _w in _v.warnings:
            print(f"  {YELLOW}⚠  {_w}{RESET}")
        if "grounding" in _v.skipped:
            print(f"  {DIM}(grounding not checked: {_v.skipped['grounding']}){RESET}")
        accepted, feedback = review(".roorules", draft)
        if accepted is not None:
            rules_content = accepted
        elif feedback is None:
            rules_content = draft
            print(f"  {DIM}.roorules skipped — using last draft as-is.{RESET}")

    # ── Step 3: ROADMAP.md ────────────────────────────────────────────────────
    roadmap_content = None
    if not args.no_roadmap:
        print(f"\n  {BOLD}Step 3/3 — ROADMAP.md{RESET}")
        feedback = ""
        while roadmap_content is None:
            draft = draft_roadmap(name, stack, vision_content, args.existing,
                                  use_claude, feedback)
            accepted, feedback = review("ROADMAP.md", draft)
            if accepted is not None:
                roadmap_content = accepted
            elif feedback is None:
                roadmap_content = draft
                print(f"  {DIM}ROADMAP.md skipped — using last draft as-is.{RESET}")
    else:
        print(f"\n  {DIM}Skipping ROADMAP.md (--no-roadmap). Run plan_week.py later.{RESET}")

    # ── Write files ───────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Writing files to {project_path}{RESET}")
    write_file(os.path.join(project_path, "VISION.md"),  vision_content,  "VISION.md")
    write_file(os.path.join(project_path, ".roorules"),  rules_content,   ".roorules")
    if roadmap_content:
        write_file(os.path.join(project_path, "ROADMAP.md"), roadmap_content, "ROADMAP.md")

    # Create CHANGELOG.md if missing
    cl_path = os.path.join(project_path, "CHANGELOG.md")
    if not os.path.exists(cl_path):
        with open(cl_path, "w") as f:
            f.write(
                "# Changelog\n\nAll notable changes to this project will be documented here.\n"
                "Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)\n\n"
                "## [Unreleased]\n\n"
            )
        print(f"  {GREEN}✓  CHANGELOG.md{RESET}")

    # Create standard dirs
    for subdir in ["logs", "release-notes", "docs"]:
        os.makedirs(os.path.join(project_path, subdir), exist_ok=True)

    # Write sovereign.json registration record
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
        print(f"  {GREEN}✓  sovereign.json{RESET}")

    # ── Next steps ────────────────────────────────────────────────────────────
    supervisor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supervisor.sh")
    print(f"""
{GREEN}{BOLD}  ✓  Bootstrap complete!{RESET}

  Review and tweak before running:
    {project_path}/VISION.md    ← product context
    {project_path}/.roorules    ← coding guardrails (add locked files as you go)
    {project_path}/ROADMAP.md   ← task list (edit task wording, add/remove tasks)

  Then run the supervisor:
    {BOLD}{supervisor} {project_path} --workers 4 --quick{RESET}

  For a deeper first pass (all tiers):
    {BOLD}{supervisor} {project_path} --workers 4{RESET}

  {DIM}Tip: As the project stabilises, add completed, stable files to the
  LOCKED FILES section in .roorules to prevent the agent from rewriting
  working code.{RESET}
""")


if __name__ == "__main__":
    main()
