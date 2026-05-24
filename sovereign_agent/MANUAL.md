# Sovereign Agent — Operator's Manual

An autonomous SDLC stack that codes, validates, and self-corrects unattended.
You commit to a work plan once; local LLMs run the loop in parallel and only
surface genuine blockers that need a human decision.

**The core idea:** your machine runs many small LLM workers simultaneously via
Ollama. A fast 4–7B model handles the majority of tasks in parallel (the "quick
pass"). Tasks it can't crack escalate through a tier ladder of progressively
larger models. The supervisor orchestrates retries, error classification, and
rule learning without any human involvement.

**Two-speed execution:**

- **Quick pass** — 8–20 parallel workers, all using the smallest GPU-resident
  model (~4.7 GB). High throughput; resolves ~70% of tasks in a first sweep.
  Tasks that require more capability are queued for the deep pass.

- **Deep pass** — fewer workers (2–6), escalating through 16B → 32B → 32B
  chain-of-thought models. Handles complex tasks the quick pass deferred.

**Coming soon:**

- **Cloud GPU burst** — slot in a remote Ollama-compatible endpoint
  (RunPod, Lambda Labs, etc.) to run the 32B tier at the same throughput
  as the local 7B tier. Set `TIER3_MODEL_URL` and the supervisor routes
  requests automatically.
- **Cloud LLM routing** — point any tier at Claude, Gemini, or GPT-4o via
  their OpenAI-compatible endpoints. Useful for tasks where a frontier model
  dramatically outperforms local alternatives, or for teams without a high-end
  local GPU.

**Reference project used throughout:** `~/Code/astro_flux`

---

## Architecture Overview

```
standup.py  →  supervisor.sh  +  work.py  →  velocity.py
  (plan)        (outer loop)    (inner loop)   (review)
                                     ↕
                    ┌────────────────────────────────┐
                    │  Ollama (local LLM server)     │
                    │                                │
                    │  Tier 1: 7B  ×N workers   ←── quick pass (parallel)
                    │  Tier 2: 16B ×M workers   ←── deep pass
                    │  Tier 3: 32B ×K workers   ←── deep pass
                    │  Tier 4: 32B R1            ←── reasoning fallback
                    │                                │
                    │  [cloud GPU / cloud LLM]  ←── coming soon
                    └────────────────────────────────┘
                    qwen_advisor.py  (error classification)
                    autofix.py       (deterministic fixes)
                    promote_rules.py (rule learning)
```

| Script | Role |
|---|---|
| `standup.py` | Morning planning — review velocity, approve today's tasks |
| `supervisor.sh` | Outer loop — spawns workers, handles escalations, loops until done |
| `work.py` | Inner loop — plans, codes, validates, retries each task |
| `velocity.py` | Dashboard — throughput, error rates, trends |
| `qwen_advisor.py` | Classifies errors and enriches retry context |
| `autofix.py` | Applies mechanical fixes (zero LLM cost) before model retries |
| `promote_rules.py` | Promotes recurring error patterns into permanent `.roorules` |
| `poll_status.sh` | Read-only status snapshot for external monitoring |

---

## Supported project types

Sovereign agent works with any stack. Validation, model prompts, and rule
templates are all stack-aware:

| Stack | Validator | Template |
|---|---|---|
| Flutter / Dart | `flutter analyze` | `--stack flutter` |
| Next.js / TypeScript | `npm test` | `--stack nextjs` |
| Swift / SwiftUI | `swift build` | `--stack swift` |
| Python | `pytest` | `--stack python` |
| Generic / Other | (auto-detect or skip) | `--stack generic` |

The supervisor and work loop are identical across stacks — only the coding
rules, .roorules template, and validation command differ.

---

## Prerequisites

### 1. Ollama

Install from [ollama.com](https://ollama.com). Verify:

```bash
ollama --version
```

Pull the models used in the tier cascade:

```bash
ollama pull qwen2.5-coder:7b-instruct-q4_K_M   # Tier 1 — ~4.7 GB
ollama pull deepseek-coder-v2:16b               # Tier 2 — ~8.9 GB
ollama pull qwen2.5-coder:32b                   # Tier 3 — ~19 GB
ollama pull deepseek-r1:32b                     # Tier 4 — ~19 GB
```

> **Storage note:** Ollama models can be stored on an external drive by setting
> `OLLAMA_MODELS` before starting the server (see below). If storing on an
> external drive, run `dot_clean /path/to/ollama/models/manifests/` after
> any model pull to remove macOS `._` resource fork files that confuse Ollama.

### 2. Python dependencies

```bash
cd ~/Code/Xanadu/sovereign_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. `.env` configuration

Copy `.env.example` if present, or edit `.env` directly:

```bash
LOCAL_MODEL_URL=http://localhost:11434

# Four-tier executor cascade — fastest to most capable
TIER1_MODEL=qwen2.5-coder:7b-instruct-q4_K_M   # ~4.7 GB, ~70% first-pass rate
TIER2_MODEL=deepseek-coder-v2:16b               # ~8.9 GB, fast dense escalation
TIER3_MODEL=qwen2.5-coder:32b                   # ~19 GB, dense 32B specialist
TIER4_MODEL=deepseek-r1:32b                     # ~19 GB, chain-of-thought reasoning

# Light tasks (planning, advising, validation checks)
PLANNER_MODEL=qwen2.5-coder:7b-instruct-q4_K_M
ADVISOR_MODEL=qwen2.5-coder:7b-instruct-q4_K_M
VALIDATOR_MODEL=qwen2.5-coder:7b-instruct-q4_K_M
```

---

## Starting the Ollama Server

The server must be running before the supervisor. Server settings have a large
impact on performance and memory usage — choose a profile that fits your machine.

### Choosing settings for your machine

The two critical variables are `OLLAMA_NUM_PARALLEL` (concurrent inference
slots) and `OLLAMA_CONTEXT_LENGTH` (tokens per slot). Their product determines
KV cache size, which determines whether the model runs on GPU or falls back to
CPU.

**Formula:**  `KV cache ≈ NUM_PARALLEL × CONTEXT_LENGTH × 0.0875 MB`  
(for the 7B model at f16; scales similarly for larger models)

| Machine | NUM_PARALLEL | CONTEXT_LENGTH | KV cache | Notes |
|---|---|---|---|---|
| MacBook Air 16 GB | 4 | 8192 | ~2.9 GB | Safe with other apps running |
| MacBook Air 32 GB | 10 | 8192 | ~7.2 GB | Tested, reliable |
| MacBook Pro 32 GB | 12 | 8192 | ~8.6 GB | Good balance |
| MacBook Pro 64 GB | 20 | 8192 | ~14.4 GB | Full parallel sweep |
| Mac Studio 96 GB | 32 | 16384 | ~46 GB | Can run 32B alongside 7B |
| Linux GPU 24 GB VRAM | 8 | 8192 | ~5.8 GB | L4/RTX 3090 tier |
| Linux GPU 80 GB VRAM | 20 | 16384 | ~29 GB | A100 — no compromises |

> **Why context length matters:** At 32768 (model default) with 20 slots, the
> KV cache alone is 35 GB — larger than a 32 GB machine's available pool.
> Ollama silently falls back to CPU, making inference ~20× slower.
> Always set `OLLAMA_CONTEXT_LENGTH` explicitly.

### macOS (Apple Silicon) — reference configuration for 32 GB

```bash
# Models on internal SSD (simplest)
OLLAMA_FLASH_ATTENTION=1 \
OLLAMA_NUM_PARALLEL=10 \
OLLAMA_CONTEXT_LENGTH=8192 \
ollama serve

# Models on external drive (e.g. Lexar SSD)
OLLAMA_MODELS=/Volumes/Lexar/ollama/models \
OLLAMA_FLASH_ATTENTION=1 \
OLLAMA_NUM_PARALLEL=10 \
OLLAMA_CONTEXT_LENGTH=8192 \
ollama serve
```

### Linux / cloud GPU

```bash
OLLAMA_FLASH_ATTENTION=1 \
OLLAMA_NUM_PARALLEL=8 \
OLLAMA_CONTEXT_LENGTH=8192 \
ollama serve
```

### Verifying the server is healthy

After starting, check the logs for these lines (good signs):

```
inference compute ... type=discrete total="26.0 GiB" available="26.0 GiB"
load_tensors: offloaded 29/29 layers to GPU      ← model is on GPU
Metal KV buffer size = 8960.00 MiB               ← KV cache on GPU
flash_attn = enabled
llama runner started in 0.81 seconds
```

If you see `offloaded 0/29 layers to GPU` or `CPU KV buffer size`, the KV cache
exceeded GPU memory. Reduce `OLLAMA_CONTEXT_LENGTH` or `OLLAMA_NUM_PARALLEL`.

### Keeping models clean

```bash
# Remove macOS resource fork ghosts from an external drive
dot_clean /Volumes/Lexar/ollama/models/manifests/

# Re-pull a model with a corrupt manifest
ollama rm qwen2.5-coder:7b-instruct-q4_K_M
# Delete any partial blobs before re-pulling to avoid EOF errors:
rm /path/to/ollama/models/blobs/sha256-<hash>-partial*
ollama pull qwen2.5-coder:7b-instruct-q4_K_M
```

---

## The Tier Cascade

`work.py` attempts each task through a four-tier model ladder. Each tier gets
up to 6 attempts before escalating to the next.

```
Tier 1 (7B)   → fast, ~70% first-pass rate, ~4.7 GB, always GPU-resident
Tier 2 (16B)  → denser model, same code domain, ~8.9 GB
Tier 3 (32B)  → strong dense specialist, ~19 GB, flushed from VRAM after use
Tier 4 (32B R1) → chain-of-thought reasoning, qualitatively different approach
     ↓
  Skipped to tier2_queue.jsonl (quick mode) or escalated (normal mode)
```

The tier advances when a model hits `PHASE_STRIKE_LIMIT` identical consecutive
errors (stuck) or exhausts `PHASE_MAX_ATTEMPTS` total attempts (thrashing).

### Time budget

Each task has a dynamic time budget: `rolling_avg_of_recent_tasks × multiplier`.
If the budget expires, the task escalates to the next tier with a 300-second
bonus window. The budget prevents runaway tasks from blocking the queue.

In parallel mode, the budget multiplier scales with worker count (capped at 6×)
to absorb Ollama queue depth.

---

## Running the Supervisor

### Sequential mode (default)

```bash
cd ~/Code/Xanadu/sovereign_agent
./supervisor.sh ~/Code/astro_flux
```

One worker, processes tasks in order. Reliable baseline, good for debugging.

### Parallel mode

```bash
./supervisor.sh ~/Code/astro_flux --workers 10
```

Spawns N workers that stride across the task list (worker K handles tasks
K, K+N, K+2N, …). All workers share the single Ollama model instance — the
model is loaded once, KV cache slots scale with `OLLAMA_NUM_PARALLEL`.

**Match `--workers` to `OLLAMA_NUM_PARALLEL`.** More workers than parallel
slots means HTTP-layer queuing in Ollama.

**Choosing worker count by machine:**

| Machine RAM | Recommended workers | OLLAMA_NUM_PARALLEL |
|---|---|---|
| 16 GB | 4 | 4 |
| 32 GB | 8–10 | 10 |
| 64 GB | 16–20 | 20 |
| 96 GB | 24–32 | 32 |

### Quick mode — tier-1 only sweep

```bash
./supervisor.sh ~/Code/astro_flux --workers 10 --quick
```

Uses only the 7B model (Tier 1). Fast — the model stays fully GPU-resident and
is never evicted. Tasks that tier-1 cannot handle are written to
`logs/tier2_queue.jsonl` instead of escalating. Restarting `--quick` skips tasks
already in the queue.

### Deep mode — tier-2+ mop-up

```bash
./supervisor.sh ~/Code/astro_flux --workers 4 --deep
```

Reads `logs/tier2_queue.jsonl` and processes only those tasks, starting at
Tier 2. Fewer workers recommended — the 16B and 32B models take longer and use
more VRAM. Clears the queue file when complete.

### Recommended two-pass workflow

```bash
# Pass 1 — fast sweep, overnight or during the day
./supervisor.sh ~/Code/astro_flux --workers 10 --quick

# Pass 2 — mop up what tier-1 couldn't handle
./supervisor.sh ~/Code/astro_flux --workers 4 --deep
```

This keeps tier-1 throughput high and defers expensive model loads to a
targeted second pass.

### Other flags

| Flag | Effect |
|---|---|
| `--features-only` | Skip tasks identified as test-writing tasks |
| `--tests-only` | Skip feature implementation tasks |
| `--max-tier N` | Cap the tier ladder at N (1-indexed); failures go to queue |
| `--workers N` | Parallel workers (default 1) |
| `--quick` | Alias for `--max-tier 1` |
| `--deep` | Process tier2_queue only, start at tier 2 |

### Stopping and resuming

Press **Ctrl+C**. The supervisor traps the signal, kills all workers, and writes
`stopped` to `logs/supervisor.status`. Restart with the same command — it
re-parses `ROADMAP.md` from the first unchecked task.

```bash
# Force-kill if Ctrl+C doesn't respond
pkill -f supervisor.sh; pkill -f work.py
```

---

## Project Onboarding

Use `dream.py` to bootstrap any new or existing project. It drafts all three
context files from a single description, lets you review each one, and writes
only what you approve.

### New project

```bash
# Greenfield Flutter app
python dream.py --project ~/Code/my_app --stack flutter \
    --idea "A social fitness tracker that logs workouts and lets friends compete"

# Next.js SaaS — use Claude API for richer output
python dream.py --project ~/Code/my_saas --stack nextjs --claude \
    --idea "A subscription-based invoice generator for freelancers"

# Python service — just answer the prompt interactively
python dream.py --project ~/Code/my_service --stack python
```

### Existing project

```bash
# The script scans the directory and uses what it finds as context
python dream.py --project ~/Code/existing_app --stack swift --existing
```

`--existing` adjusts the tone: VISION.md frames the project as an ongoing
codebase, the ROADMAP starts with audit/cleanup tasks, and .roorules marks the
existing API layer as locked.

### What gets written

| File | Purpose |
|---|---|
| `VISION.md` | Product context injected into planning prompts |
| `.roorules` | Coding guardrails injected into every executor prompt |
| `ROADMAP.md` | Initial task list (20–30 tasks, dependency-ordered) |
| `CHANGELOG.md` | Running change log (Keep a Changelog format) |
| `sovereign.json` | Registration record for the supervisor |

### Workflow: ideation → automation

```bash
# 1. Bootstrap the project
python dream.py --project ~/Code/my_app --stack nextjs

# 2. Review and edit the three files the LLM drafted:
#    VISION.md  — fill in any details the model got wrong
#    .roorules  — add API signatures and locked file paths you know
#    ROADMAP.md — reorder tasks, split broad ones, add specifics

# 3. Run a quick tier-1 sweep
./supervisor.sh ~/Code/my_app --workers 8 --quick

# 4. Review velocity, run the deep pass on failures
./supervisor.sh ~/Code/my_app --workers 4 --deep

# 5. Repeat standup → quick → deep until the roadmap is complete
python standup.py --project ~/Code/my_app
```

### Using Claude API for ideation

`dream.py` defaults to Ollama (the same model as your TIER4_MODEL). For
higher-quality VISION.md and ROADMAP drafts, pass `--claude`:

```bash
# In sovereign_agent/.env:
ANTHROPIC_API_KEY=sk-ant-...

# Then:
python dream.py --project ~/Code/my_app --stack nextjs --claude
```

Claude is used only for the three ideation drafts. The supervisor, work loop,
and all retry logic continue to use your local Ollama models.

### `dream.py` vs `init_project.py`

`init_project.py` writes static template files with placeholder brackets for
you to fill in manually. `dream.py` wraps it with an LLM drafting step that
fills in real content. Use `dream.py` when starting fresh; use `init_project.py`
when you want to add sovereign.json and directory structure to an already
written VISION.md.

---

## 1. `standup.py` — Morning Planning

Runs an interactive session before the day's work. Shows yesterday's velocity,
today's planned tasks, and tomorrow's queue. You review and approve.

```bash
python standup.py --project ~/Code/astro_flux
```

| Key | Action |
|---|---|
| `y` | Approve today's plan and exit |
| `e` | Edit today's task list (4B model rewrites/refines) |
| `p` | Pivot tomorrow's plan |
| `f` | Fill tomorrow from backlog up to 22 tasks |
| `q` | Quit without approving |

---

## 2. `work.py` — Task Executor

Called by the supervisor. Can also be run directly.

```bash
# Normal (supervisor handles this)
python work.py --project ~/Code/astro_flux

# Resume at task 4 after a manual fix
python work.py --project ~/Code/astro_flux --start-at 4

# Dry-run — preview tasks without executing
python work.py --project ~/Code/astro_flux --dry-run

# Tier-1 only, with parallel stride
python work.py --project ~/Code/astro_flux --worker-id 0 --stride 10 --quick
```

### What it does per task

1. **Planner** (7B) selects the most relevant source files (up to 10)
2. **Executor** (current tier model) generates complete file implementations as JSON
3. Pre-write safety scanner rejects known-bad patterns before anything hits disk
4. Writes changes, skipping locked files
5. Runs the first available validator: `flutter analyze` → `pytest` → `npm test`
6. On failure: `autofix.py` applies mechanical fixes, then `qwen_advisor.py`
   classifies the error and enriches context for the next attempt
7. On success: marks the task `[x]` in `ROADMAP.md`
8. On tier exhaustion: skips task (quick mode → queues for deep; normal → escalates)

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All tasks attempted |
| `1` | Unexpected error |
| `2` | Structural escalation — human or Claude intervention required |
| `130` | Interrupted (Ctrl+C) |

### Key tunables (top of `work.py`)

| Constant | Default | Purpose |
|---|---|---|
| `PHASE_STRIKE_LIMIT` | 1 | Same-error streak before advancing tier |
| `PHASE_MAX_ATTEMPTS` | 6 | Total attempts per tier before advancing |
| `BUDGET_MULTIPLIER` | 1.7 | Budget = rolling_avg × multiplier |
| `BUDGET_CEILING_S` | 1200 | Hard cap per task (20 min) |
| `BUDGET_FLOOR_S` | 120 | Minimum budget (2 min) |

---

## 3. `velocity.py` — Dashboard

```bash
python velocity.py --project ~/Code/astro_flux         # last 7 days
python velocity.py --project ~/Code/astro_flux --days 1 # today only
```

Reads `logs/velocity.jsonl` and prints:
- Daily table: done / failed / total, success rate, avg retries
- Top recurring error types (highest-ROI targets for adding rules)
- List of failed tasks

**Reading the output:**
- Success rate ≥ 80% → healthy. Below 50% → something structural is wrong.
- Avg retries at 1.0× → every task passed first attempt. 5×+ → add a rule.
- Same error in 3+ tasks → add an `ERROR_HINTS` or `STRUCTURAL_PATTERNS` entry
  to `work.py`.

---

## 4. `promote_rules.py` — Rule Learning

Runs automatically after each supervisor batch. Scans `logs/velocity.jsonl`
for error patterns that appear across multiple tasks and promotes them into the
project's `.roorules` file.

```bash
python promote_rules.py --project ~/Code/astro_flux --threshold 2
```

`--threshold N` — promote a pattern after it appears in N or more tasks (default 2).

---

## File Reference

```
sovereign_agent/
├── dream.py                 Bootstrap: LLM-drafts VISION.md + .roorules + ROADMAP.md
├── init_project.py          Bootstrap: static templates (no LLM; use dream.py instead)
├── work.py                  Core task executor
├── standup.py               Morning planning session
├── supervisor.sh            Outer loop (parallel workers, escalation, learning)
├── velocity.py              Velocity dashboard
├── qwen_advisor.py          Error classifier and hint enricher
├── autofix.py               Deterministic mechanical fixes
├── promote_rules.py         Pattern → rule promotion
├── plan_week.py             Backlog planning helper
├── poll_status.sh           Read-only status snapshot
├── MANUAL.md                This file
├── requirements.txt         Python dependencies
└── .env                     Model + endpoint configuration

astro_flux/                  (your project)
├── ROADMAP.md               Task source of truth — checkboxes
├── .roorules                Coding rules injected into every executor prompt
├── VISION.md                Product context (first 1500 chars injected)
└── logs/
    ├── supervisor.status        Current loop state
    ├── supervisor.log           Timestamped supervisor event log
    ├── escalate.md              Latest escalation report (normal mode)
    ├── escalate-wN.md           Per-worker escalation (parallel mode)
    ├── velocity.jsonl           Per-task outcome records
    ├── task_traces.jsonl        Full prompt→output→result traces (QLoRA data)
    ├── tier2_queue.jsonl        Tasks queued by --quick for --deep follow-up
    └── YYYY-MM-DD-work[-wN].log Per-worker attempt detail log
```

---

## Escalation Flow (Normal Mode)

When a task exhausts all tiers, `work.py`:

1. Writes a rich report to `logs/escalate.md` (task, cause, errors, resume command)
2. Exits with code 2
3. `supervisor.sh` writes `needs_fix:N` to `logs/supervisor.status` and pauses
4. Fix the issue (Claude or manually), then write `fixed:N` to the status file
5. Supervisor resumes

```bash
# Resume after a manual fix
echo "fixed:4" > ~/Code/astro_flux/logs/supervisor.status
```

Structural error patterns that trigger escalation:
- `uri_does_not_exist` — missing package / `flutter pub get` not run
- `extends_non_class` / `non_type_as_type_argument` — Riverpod type errors
- `AsyncError` called with wrong argument count — caught by `ERROR_HINTS`
- Zero output for 3+ attempts — all candidate files are locked

---

## Troubleshooting

### All workers timing out after 2 minutes

The model is running on CPU. KV cache exceeded GPU memory. Reduce
`OLLAMA_CONTEXT_LENGTH` or `OLLAMA_NUM_PARALLEL` and restart the server.
Verify with: `load_tensors: offloaded 29/29 layers to GPU` in the Ollama log.

### `Error: EOF` when pulling a model

Caused by partial blobs left from a previous interrupted download:

```bash
ls /path/to/ollama/models/blobs/ | grep partial
rm /path/to/ollama/models/blobs/sha256-<hash>-partial*
ollama pull <model>
```

### Repeated `bad manifest name` warnings in Ollama log

macOS resource fork files (`._`) on an external drive:

```bash
dot_clean /Volumes/YourDrive/ollama/models/manifests/
```

### Workers hitting planner timeout (500 after 5m)

Too many workers for the GPU to serve within the budget window. Reduce
`--workers` or decrease `OLLAMA_NUM_PARALLEL` so the queue drains faster.
The 7B model can reliably handle 8–10 concurrent sessions on 32 GB unified memory.

### `work.py` escalating on the same task repeatedly

Read `logs/escalate.md`. Common fixes:

| Cause | Fix |
|---|---|
| All candidate files are locked | Implement manually, mark `[x]` in ROADMAP |
| `uri_does_not_exist` | Run `flutter pub get` in project directory |
| Model keeps producing same wrong pattern | Add to `STRUCTURAL_PATTERNS` or `ERROR_HINTS` in `work.py` |
| Task is too broad | Split into 2–3 narrower tasks with explicit file paths |

### Supervisor won't stop with Ctrl+C

```bash
pkill -f supervisor.sh; pkill -f work.py
```

### tier2_queue.jsonl has many duplicates

Multiple `--quick` restarts before the dedup logic was in place:

```bash
python3 -c "
import json
path = 'logs/tier2_queue.jsonl'
seen, out = set(), []
for line in open(path):
    r = json.loads(line)
    if r['task'] not in seen:
        seen.add(r['task']); out.append(line.strip())
open(path,'w').write('\n'.join(out)+'\n')
print(len(out), 'unique tasks')
"
```
