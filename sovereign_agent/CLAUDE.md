# Sovereign Agent — Claude Context

## What This Is — READ FIRST
The **sovereign_agent** harness (`plan_week.py` / `work.py` / `supervisor.sh` / `orchestrate.py`) is **language- and project-agnostic**. It is an autonomous SDLC engine, not a Flutter tool. It has already driven **Flutter (Dart)**, **Go**, and is **currently driving a TypeScript / React** project. Nothing about the supervisor, tiers, escalation, or task loop is tied to a language.

**Everything project-specific comes from the project's own directory, not from this file:**
- `<project>/.sovereign_config.json` — locked files, `validate_commands`, file-length cap, etc.
- `<project>/ROADMAP.md` and `<project>/task_graph.json` — the work to do.
- `<project>/spec/` and `<project>/src/.../API.md` — the contracts tasks are written against.
- `<project>/logs/` — live state: `supervisor.status`, `escalate.md`, `velocity.jsonl`, the dated `*-work.log`.

**To answer "how is the run going?" always inspect the *active project's* `logs/`, never assume Flutter.** Sections below that name Dart files, `lib/`, `pubspec.yaml`, iOS/Android, Flame, `BlendMode.add`, etc. are **examples from the Flutter incarnation** — translate them to the active project's stack (e.g. TypeScript: `src/`, `package.json`, `tsc`/`vitest`, `npm run typecheck:sim`).

### Active project (update when it changes)
- **galaxican** — Flutter/Flame, **fresh clean pure-Dart sim core** ported from the tested TS sim. Path: `~/Code/galaxican`. **Integration branch is `master`, not `main`** — always pass `MAIN_BRANCH=master` (the pre-run guard checks it). Validate via `flutter analyze` + `flutter test` (see the project's `.sovereign_config.json` `validate_commands`). Current focus as of 2026-07-18.
  - **Strategy (decided 2026-07-18):** the game design + mechanics are proven in the TS port (`~/Code/GalaxicanJS`: 50 pure-fn sim files, 57 conformance cases, all green). We are porting that sim structure-for-structure into pure Dart (`lib/sim`, `lib/ai`, `lib/level`), gated by the SAME conformance behavior ported to `flutter test`. Flame is used for RENDERING only (its strength; where qwen3 is accurate) — the sim core imports NO Flame/Flutter/Riverpod. The old 593-file Riverpod app is legacy/removed, not a foundation.
  - The canonical **contract is `spec/`** (copied from GalaxicanJS, locked). Port tasks are written against it exactly.
  - **Reference-source injection (reliability lever for PORTS):** the TS source is mirrored read-only in `reference/ts/src/…` and `work.py` auto-includes the file a task names ("port from `src/sim/setTarget.ts`" → injects `reference/ts/src/sim/setTarget.ts` into the worker's context). Without this the worker can't see the source it's told to port and *guesses* APIs (e.g. inventing `Vec2.normalize()`). For any port project, mirror the source into `reference/` — it turns "invent" into "translate" and is the single biggest reliability win on trivial port tasks.
- Prior incarnations: **GalaxicanJS** (TS/React — the reference sim + the source of the portable harness lessons) and a **Go** build. Same game design (below), different stacks.

### Current run mode — SINGLE MODEL (set 2026-07-17)
- **qwen3-coder:30b is the ONLY model in use.** All four tiers + planner + advisor + race are pinned to `qwen3-coder:30b` in `~/Code/Xanadu/sovereign_agent/.env`. Claude escalation stays OFF (`CLAUDE_ENABLED=0`), race is OFF. The escalation ladder therefore just retries the same model — no Gemma, no qwen2.5, no cloud.
- To change models: edit the `SINGLE-MODEL MODE` block in `.env` (or delete the block to restore work.py's multi-model ladder defaults). Do not scatter model overrides elsewhere.
- Tag must match `ollama list` exactly — it is `qwen3-coder:30b` (18 GB), not `qwen3.6:35b` or `qwen2.5-coder:*`.

### Run hygiene rules (enforced by work.py pre-run guards — do not defeat them)
- **NEVER edit/commit the project repo while a run is live — and a run is "live" until `pkill` confirms it.** The harness cycles `git checkout -f <MAIN_BRANCH>` + `git clean -fd` between every task; any uncommitted work in the tree is wiped, and commits race the harness's git ops and get stranded. Ctrl-C in the terminal only kills the FOREGROUND process — the supervisor + workers survive and keep touching the repo. So a stop is NOT done until: `pkill -9 -f work.py; sleep 1; ps aux | grep '[w]ork.py' | wc -l` reads **0**. Whenever you ask the user to Ctrl-C, ALWAYS pair it with that pkill+verify. Only scaffold/commit once processes are confirmed dead. (Learned 2026-07-18: a full p3–p9 scaffold was wiped because a "stopped" run still had 4 live work.py processes.)
- **`logs/` is generated, untracked, and gitignored — never commit it, never let it block a run.** The dirty-tree guard explicitly ignores `logs/`. Logs stay on disk so the harness can learn from them; discard them only when explicitly done with a run.
- **Never regenerate `task_graph.json` mid-run.** work.py drives off `ROADMAP.md` (`parse_all_tasks()`); the graph is the durable DAG record maintained only by `make_graph.py` / `plan_week.py`. Regenerating it during a run clobbers completed-phase state and desyncs it from ROADMAP.
- **Commit setup edits (ROADMAP, config, locked-test fixes) to `main` immediately.** Uncommitted edits get wiped by the next task branch's forced `git checkout -f main`.

## Task Discipline (READ BEFORE WRITING ANY TASK — applies to every project/stack)
> **A task is right-sized when the worker cannot fail on design or integration —
> only on correctness — because every input, output, and dependency it needs is
> named explicitly and it has exactly one thing to do.**

This is not Galaxican-specific; it is how you write tasks for any coding agent in
any language. The model rarely fails because it can't code — it fails because the
*task* let it guess. Do not keep re-asking for smaller, clearer tasks; write them
that way the first time. The four reflexes:
1. **One file, one concern.** If the description needs an "and", split it.
2. **Exact contracts.** Name the input signature, the output signature, the exact
   import (module path + symbol) for every dependency, and the real fields of any
   type touched — then forbid the known-bad guesses by name.
3. **Isolate glue into pure functions.** Integration logic (framework↔domain,
   UI↔state) hallucinates most; give it a fixed signature in its own file and keep
   shells thin (they only compose things that already exist).
4. **Concrete done-gate that actually runs.** A command/test that passes — never
   "renders nicely". In this harness a per-task behavioural gate is only executed
   when written as a `task gate: <cmd> passes` clause at the END of the task line
   (parsed by `_extract_task_gate`); a gate mentioned in "done when:" prose is
   NOT run, so the task can be marked done on typecheck alone. Typecheck passes
   broken string/geometry output — pure functions that return strings/data need a
   `task gate:` behavioural test. After adding a gate, confirm it fails on the
   current broken output before trusting a green.

The tell that a task is wrong (not the model): a worker fails 2+ times on "X
doesn't exist" or on the same integration seam. Rewrite/split the task; don't just
retry a bigger model. **Full doctrine + worked example: `docs/TASK_DISCIPLINE.md`.**

## General Rules
- **NEVER use `mv` for moving files or directories.** Always use `rsync` instead. `mv` across devices/drives can behave unexpectedly, appear to hang, and is not resumable. Use:
  ```
  rsync -ah --progress <src> <dst> && rm -rf <src>
  ```
- **ALWAYS generate the smallest possible task — both in scope AND complexity.** A project's file-length limit (e.g. Galaxican's 150-line cap) is a ceiling, not the sizing rule — it doesn't mean a task can bundle multiple changes as long as the result stays under it. `plan_week.py`'s prompt enforces: exactly one file per task, exactly one unit of work per task (one method, one class, one fix — never "X and Y"). `work.py` no longer merges same-file tasks into one combined call either (removed 2026-07-10 — it directly fought this principle and made tasks harder to complete, not safer); same-file cascade protection is now handled by deferring a task to the retry pass if its immediate predecessor on the same file failed, instead of merging them.

  **Task sizing checklist:**
  - ✓ Does this task depend on a single clear contract (spec section, API signature)?
  - ✓ Can a worker complete it in one attempt without inventing integration patterns?
  - ✓ Is the task so focused that even the wrong implementation will typecheck (or fail obviously)?
  - ✗ DON'T: "Build a canvas component" (too vague; the worker will invent an API)
  - ✗ DON'T: "Render game entities, add animations, wire gestures, handle overlays" (5 tasks bundled)
  - ✗ DON'T: "Implement game loop + rendering" (couples state management and visual layer)
  - ✓ DO: "Implement useGameState hook — just state + tick, no rendering"
  - ✓ DO: "Render board given game state (no logic, no animations)"
  - ✓ DO: "Convert tap position to orderTapStar/orderTapWorld (pure function)"
  - ✓ DO: "Wire React Native touch events to lasso detector"

  The goal: **each task should be so small that the worker can't introduce cross-cutting concerns, can't invent new APIs, and can only fail on correctness (which the test gate catches), not design.**

- **ALWAYS write tasks with three elements: SPECIFIC inputs, SPECIFIC outputs, and CLEAR DEFINITION OF DONE.** A vague task causes hallucination. Every task must name:
  1. **Input contract**: exact types/signatures of what the worker receives (e.g. "accepts `(g: GameState, pos: Vec2)`, no other args")
  2. **Output contract**: exact types/signatures of what the worker must produce (e.g. "returns `string` (SVG path data), never null")
  3. **Done criteria**: a specific test or acceptance gate, not a vague instruction (e.g. "done when: `npm run test:locked` passes for p10_render.test.ts", NOT "done when: component renders nicely")

  **Examples:**
  - ✗ BAD: "Implement the Star renderer component" (What props? What does it render? What counts as 'done'?)
  - ✓ GOOD: "Implement StarDisplay(props: {star: Star; game: GameState}): ReactNode. Renders star circle + glow via drawStar; pulsing glow animation from spec/08-presentation.md §3. — done when: npm run typecheck:sim passes, p10_render.test.ts has no render errors on start"
  - ✗ BAD: "Write a draw function for game entities" (Too many entities, no specificity)
  - ✓ GOOD: "Implement drawStar(s: Star, color: string): string — returns SVG path data string per spec/08-presentation.md §3. Includes glow circle (radius+4), and if owned, production arc + HP ring. — done when: npm run typecheck:sim passes, function returns valid SVG path strings in all test cases"

  Vague tasks force models to guess at inputs/outputs/APIs, which causes import errors, type mismatches, and hallucinated function calls. Specific tasks with clear gates let workers fail obviously and fast.

- **NEVER run feature tasks and test tasks in the same worker session.** There's features, and there's tests — a test task must never run in the same session as the feature task it depends on, whether sequential or parallel. Mixing them lets a test execute against an implementation that hasn't landed yet (or that a different parallel worker is still mid-edit on).
  - `./supervisor.sh <project> --full` enforces this automatically — it runs a complete features-only session (quick sweep + deep mop-up) to completion, then a complete tests-only session. Never interleaved.
  - If invoking `work.py` or `supervisor.sh` directly (not via `--full`), always pass `--features-only` or `--tests-only` explicitly.
  - `supervisor.sh` prints a warning if run with neither `--full` nor a mode flag, since that mixes both task types in one session.

## Code Environment Labels
Every code snippet must be labelled with one of:
- **[Mac | local]** — plain Mac terminal, no venv needed (e.g. git, gcloud, docker commands)
- **[Mac | venv]** — Mac terminal with sovereign_agent venv active: `cd ~/Code/Xanadu/sovereign_agent && source .venv/bin/activate`
- **[PC | WSL | venv]** — Windows WSL terminal with sovereign_agent venv active: `cd ~/Code/sovereign_agent && source .venv/bin/activate`
- **[L4 VM | ssh]** — SSH'd into sovereign-gpu-l4: `gcloud compute ssh sovereign-gpu-l4 --zone us-west1-a --project astro-flux-spyderboy`
- **[L4 VM | venv]** — Inside the L4 VM with venv active: `cd ~/sovereign_agent/sovereign_agent && source ../.venv/bin/activate`

**Formatting rule:** The environment label goes in plain text BEFORE the code block, never as a comment inside it. Code blocks must be clean and copy-pasteable with no inline comments added by Claude.

**CRITICAL:** DO NOT include comments in code blocks users will copy-paste. Comments break shell interpretation, Python heredocs, and multiline commands. If the code needs explanation, write it in plain text *outside* the code block, not inside it. This applies especially to:
- Bash scripts with `# comment` lines (break command interpretation in some contexts)
- Python scripts pasted into heredoc (`<< 'EOF'`) — comments inside cause parsing errors
- Any multiline command where # might be interpreted as end-of-statement

Bad: code block with `# this is a comment` inside  
Good: explanation text before the block, then clean code with zero comments

---

## Machine Map
| Machine | Role | Tier | Model | Path |
|---------|------|------|-------|------|
| Mac | Orchestrator + tier-1 worker | 1 | qwen2.5-coder:7b | `~/Code/Xanadu/sovereign_agent` |
| PC (WSL) | Tier-1 worker | 1 | qwen2.5-coder:7b | `~/Code/sovereign_agent` |
| sovereign-gpu-l4 (us-west1-a) | Tier-2 worker | 2 | qwen2.5-coder:32b + Claude | `~/sovereign_agent/sovereign_agent` |

**GPU quota**: 1 GPU globally → only sovereign-gpu-l4 can run. ollama-t4 cannot start while L4 is running.

---

## Product: Galaxican (game design — shared across all incarnations)

The **game design is stack-independent**; it's been built in Flutter, Go, and now TypeScript/React. Read the active project's own vision/spec before planning (e.g. `~/Code/galaxican/VISION.md` for Flutter, `~/Code/GalaxicanJS/spec/` for the TS port).

A real-time strategy / idle game. Core loop:
1. Stars produce Motes automatically (3 tiers, higher = faster)
2. Player directs Motes toward Stars
3. 10 Motes auto-fuse into 1 Vector
4. Vectors capture Stars

**v1.0 definition of done**: Stars produce Motes → player directs → 10 auto-fuse into Vector → Vectors capture Stars.

**What does NOT exist in v1**: Novas, deep combat system, leaderboards, GCP backend (future).

Aesthetic (non-negotiable): dark background, high-saturation neon glows, additive blending, no flat UI chrome inside the game canvas. (In Flutter this meant `BlendMode.add` + no Material widgets; translate to the equivalent in the active stack.)

**Tech stack — varies by incarnation:**
- **GalaxicanJS (current):** TypeScript / React, validated with `tsc` + `vitest` (`npm run typecheck:sim`, `npm run test:locked`). Path: `~/Code/GalaxicanJS`.
- **galaxican:** Flutter/Flame, targeting iOS + Android. Path: `~/Code/galaxican`.
- **(Go build):** earlier incarnation.
- Cloud backend (GCP: Firestore, Pub/Sub, Cloud Run Jobs, GCE GPU VM) is the harness/orchestration layer, shared and future-facing — not part of game v1.

**Sovereign agent path (the harness):** `~/Code/Xanadu/sovereign_agent`

---

## Cloud Run Architecture (the goal)

```
orchestrate.py ──► Pub/Sub: tasks-tier1 ──► Tier-1 workers (Cloud Run Jobs, T4 GPU)
                                                    │ fail
                                                    ▼
                                            Pub/Sub: tasks-tier2 ──► Tier-2 workers (GCE VM, L4 GPU)
                                                                              │ fail
                                                                              ▼
                                                                      Claude inline escalation
```

- **Tier 1** — `qwen2.5-coder:7b-instruct-q4_K_M`, Cloud Run Jobs with embedded Ollama (T4, OLLAMA_EMBEDDED=true). Many parallel containers, each handles one task and exits.
- **Tier 2** — `qwen2.5-coder:32b` + inline Claude escalation, GCE VM with L4 GPU (OLLAMA_EMBEDDED=false, external OLLAMA_URL). Fewer workers.
- **Orchestrator** — `orchestrate.py` reads `task_graph.json`, tracks state in Firestore, publishes to Pub/Sub, listens on `task-results` for completions.
- **Results** — workers push outcomes to `task-results` topic; orchestrator unlocks dependents and updates Firestore.

**GCP project:** `astro-flux-spyderboy`  
**Ollama VM internal IP:** `10.138.0.2` (L4 VM, `OLLAMA_URL=http://10.138.0.2:11434`)  
**Docker image:** `gcr.io/astro-flux-spyderboy/sovereign-worker:latest`

---

## Current State — always read from the active project's `logs/`
**Do not trust a hardcoded snapshot here.** For live status, inspect the active project's directory:
- `<project>/logs/supervisor.status` — `running:N` / `done` / `stopped`
- `<project>/logs/escalate.md` — the task currently blocked on a human (if any)
- `<project>/logs/<date>-work.log` — per-task attempts and PASSED/FAILED
- `<project>/logs/velocity.jsonl` — per-task outcomes, attempts, model
- `<project>/task_graph.json` — total task count (note: `status` fields may all read `pending` even mid-run; completion lives in the work log)

### Key Session Learnings (stack-agnostic)
- **Do not run 4+ local parallel workers** — local 32B/R1 models (19 GB) starve each other of GPU RAM when loaded concurrently. All heavy attempts timeout in a loop. The cloud run is the fix (each tier gets its own GPU instance).
- **Locked type/API files cause "undefined member" loops.** The single most common failure across *every* stack: a worker generates code referencing a member of a locked model/type it can't see, so it invents fields and fails repeatedly (Flutter: `GalaxicanGame.new` positional-arg errors from locked `galaxican_game.dart`; TS: `Property 'pendingTowerHeal' does not exist on type 'Squad'` from locked model files). **Fix:** feed the locked type/API signatures in as context. Top error codes are always some form of `undefined_*` / `does not exist`.
- **Deprecated / drifted API patterns** — models regenerate known-bad calls even when bad-pattern filters catch them (Flutter example: `.withOpacity()`, wrong `Game.update()` signature). If a task escalates repeatedly on a real logic or API bug, implement it manually.
- **Stub detection** — anti-stub rules live in the project's rules file (`.roorules` / equivalent). Every task carries a `— done when:` acceptance clause plus a `task gate:` test.

### Flutter-era specifics (historical — only relevant to `~/Code/galaxican`)
- `game_widget.dart` locked but tasks kept writing to it — decide whether to unlock or reroute.
- `lib/components/combat_result_label_component.dart` referenced by generated code but didn't exist — add to context or create it.

---

## Cloud Run Startup Sequence

### Prerequisites (check once)
1. `galaxican` pushed to GitHub (`PROJECT_REPO` env var must be set — **currently missing, no git remote**)
2. Docker image built and pushed to GCR
3. Pub/Sub topics and subscriptions created (`setup_pubsub.sh`)
4. Ollama VM running (`start_ollama_vm.sh` or `gcloud compute instances start ollama-server`)

### Full Launch Sequence

**Step 1 — Push galaxican to GitHub** [local]
```bash
cd ~/Code/galaxican
git init && git remote add origin https://github.com/joseantoniolicon/galaxican.git
git push -u origin main
```

**Step 2 — Build and push the Docker image** [local, requires Docker + gcloud auth]
```bash
cd ~/Code/Xanadu/sovereign_agent
docker build -t sovereign-worker .
docker tag sovereign-worker gcr.io/astro-flux-spyderboy/sovereign-worker:latest
docker push gcr.io/astro-flux-spyderboy/sovereign-worker:latest
```

**Step 3 — Ensure Pub/Sub is set up (safe to re-run)** [local]
```bash
cd ~/Code/Xanadu/sovereign_agent
GCP_PROJECT=astro-flux-spyderboy ./gcp/setup_pubsub.sh
```

**Step 4 — Start the Ollama VM if stopped** [local]
```bash
gcloud compute instances start ollama-server --zone us-central1-a --project astro-flux-spyderboy
```

**Step 5 — Start the orchestrator** [venv]
```bash
cd ~/Code/Xanadu/sovereign_agent
source .venv/bin/activate
python orchestrate.py --project ~/Code/galaxican
```

**Step 6 — Launch tier-1 workers (separate terminal)** [local]
```bash
cd ~/Code/Xanadu/sovereign_agent
PROJECT_REPO=https://github.com/joseantoniolicon/galaxican.git \
./gcp/run_workers.sh --workers 4
```

**Step 7 — Launch tier-2 worker on the L4 VM** [gcloud ssh: ollama-server]
```bash
docker run --rm \
  -e WORKER_TIER=2 \
  -e OLLAMA_EMBEDDED=false \
  -e OLLAMA_URL=http://localhost:11434 \
  -e ANTHROPIC_API_KEY=<key> \
  -e PROJECT_REPO=https://github.com/joseantoniolicon/galaxican.git \
  -e FIRESTORE_PROJECT_ID=astro-flux-spyderboy \
  gcr.io/astro-flux-spyderboy/sovereign-worker:latest
```

### Monitoring
```bash
# Status snapshot [venv]
./poll_status.sh

# Orchestrator status [venv]
python orchestrate.py --project ~/Code/galaxican --status

# Velocity dashboard [venv]
python velocity.py --project ~/Code/galaxican
```

---

## Wrapper Scripts (handle venv automatically)
These activate the venv internally — no need to activate manually:
- `./work --project ~/Code/galaxican` → runs `work.py`
- `./standup --project ~/Code/galaxican` → runs `standup.py`

For `orchestrate.py`, `velocity.py`, `make_graph.py` — either activate the venv first or call them with the venv's Python directly.

---

## Locked Files
Locked files (cannot be written by workers) are **defined per-project** in `<project>/.sovereign_config.json` — always read that file for the authoritative list; do not assume the Dart paths below.

The pattern is the same across stacks: core engine/model/config/type files + the manifest + `ROADMAP.md` are locked so workers can't rewrite the contracts they build against.

*Flutter example (`~/Code/galaxican`):* `lib/game/galaxican_game.dart`, `lib/game/game_widget.dart`, `lib/game/game_core.dart`, `lib/models/*`, `lib/config/*`, `lib/components/mote_component.dart`, `lib/components/particle_component.dart`, `pubspec.yaml`, `ROADMAP.md`.

*TS example (`~/Code/GalaxicanJS`):* see its `.sovereign_config.json` (locked model/type files under `src/`, `package.json`, `ROADMAP.md`).
