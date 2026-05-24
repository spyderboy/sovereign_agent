# Xanadu: Your Autonomous SDLC Stack

**What It Is**

Xanadu is a local software development automation system that runs an AI-powered coding loop against any project that has a `ROADMAP.md`. You define the work; the system executes it unattended, validates results, and only surfaces blockers that genuinely require human judgment. The goal is to compress the distance between "I have a backlog" and "it's done" to the cost of a morning check-in and occasional unblocking.


`plan_week.py` — **The AI Product Manager**

This is where the work originates. You run it once per project — or whenever you need to refill the backlog — and it generates an entire `ROADMAP.md` from scratch. It reads `VISION.md`, understands what's already been done, and plans concrete engineering tasks forward. It acts as a PM who has read the spec and knows the codebase.

```bash
python plan_week.py --project ~/Code/astro_flux --weeks 2 --tasks-per-day 22
```

That one command generates 2 weeks × 5 days × 22 tasks = 220 tasks, all from the local 4B model. The 190 tasks currently in Astro Flux came from a run like this.

It plans incrementally — each week is generated with the previous week's output fed back as context, so the model never repeats tasks or plans things out of order. Week 2 knows what Week 1 already covered. It also enforces strict task sizing at generation time, explicitly forbidding "Build X system" or "Integrate X with Y" shapes and requiring every task to touch 1–2 files and be doable in a single model pass. When tasks slip through at the wrong granularity, the execution loop pays for it later in the form of budget timeouts.

One non-obvious detail: the date headers in `ROADMAP.md` are purely cosmetic. `work.py` reads all unchecked items in order regardless of date. The dates exist for your readability only, not as execution gates.


`standup.py` — **The Daily Command Center**

This is the most interactive part of the system and probably the most underrated. It's not just an approval step — it's where you actively manage the shape of the work before committing to it. The whole session takes 2–5 minutes and it does several distinct things.

**Velocity data first, then the plan.** Before you see a single task, standup loads the last 3 days of `velocity.jsonl` and prints a performance snapshot: success rate, average retries per task, total tasks completed, and the top recurring error types. This is intentional — you see how yesterday went before you decide what to commit to today. If yesterday's error rate was high, you can trim the plan or spend a minute adding a `BAD_PATTERNS` rule before the loop starts. If the system is in a good groove, you approve quickly and walk away.

**Two-day horizon.** Standup shows both today's tasks and tomorrow's, not just today's. Tomorrow's queue gets a warning if it's below 22 tasks, telling you exactly how many to pull. This keeps you from getting blindsided by an empty queue mid-session.

**Five options at the menu:**

- `y` — Approve. Locks the plan by writing `today_approved.md`, which `work.py` reads instead of the raw ROADMAP. Also writes `.roo-mission.md` with the first task as a mission brief (including all remaining tasks and a `.roorules` reference) — the integration point with the older Roo Code workflow.
- `f` — Fill tomorrow. If tomorrow has fewer than 22 tasks, pressing `f` physically moves tasks from future backlog dates into tomorrow's section — removing them from their source date and appending them to tomorrow's. It's not a display trick; it edits `ROADMAP.md` directly. You can press `f` multiple times to review each batch.
- `p` — Pivot tomorrow. You type a natural-language direction ("focus on the offline sync layer instead of audio" or "add these specific tasks: X, Y, Z") and the 4B model rewrites tomorrow's tasks accordingly. You see the new list before confirming, and nothing changes until you press `y` at the confirmation prompt. This is how you steer the system day-to-day without manually editing the ROADMAP.
- `e` — Edit today. Goes through each of today's open tasks one by one. Press Enter to keep it as-is, type a replacement to rewrite it, or type `x` to drop it. You can also add a new task at the end. Every change writes back to `ROADMAP.md` immediately.
- `q` — Exit without approving, leaving the day unstarted.

The combination of these options means standup is genuinely the daily steering interface for the whole system. `plan_week.py` sets the direction for weeks; standup adjusts it day by day based on what actually happened yesterday.


`supervisor.sh` — **The Outer Loop**

The shell script you leave running in a terminal tab. It calls `work.py`, watches the exit code, and handles three outcomes: normal completion (loop immediately), escalation (pause and wait for a fix), or stuck (give up after 8 unresolved escalations). When escalation happens, it writes `needs_fix:N` to `logs/supervisor.status` and waits up to 30 minutes for `fixed:N` to appear — Claude's cue to intervene.


`work.py` — **The Engine**

For each unchecked task in `ROADMAP.md` it runs a three-tier code generation cascade. Tier 1 (gemma4:26b) handles the majority of tasks. If it produces the same error twice, the system advances to Tier 2 (deepseek-coder-v2:16b). If Tier 2 gets stuck, it escalates to Tier 3 (qwen3.5:35b). Each tier gets up to 6 total attempts before being considered stuck.

On every failure it applies `autofix.py` mechanical fixes first (zero API cost — deterministic transformations like adding `@override`), then calls the qwen advisor to classify the error, enrich the retry context, and draft new `.roorules` entries. Three additional systems enforce code quality at the pre-write and post-write stages:

**LOCKED_FILES** — a set of file paths that can never be overwritten by the AI. Grows over time as files are manually verified and stabilized. The AI can see locked files as context but cannot touch them.

**BAD_PATTERNS** — a pre-write regex scanner that checks every generated file before it touches disk. Known-wrong patterns (e.g. `super.update(dt)`, `const Vector2(...)`, `Colors.magenta`) are rejected and the violation message is fed back as a precise correction on the next attempt. This is the system's immune response to recurring mistakes.

**ERROR_HINTS** — maps `flutter analyze` error codes to targeted guidance. When validation fails and the output matches a known pattern, the hint is prepended to the errors on the next retry so the model gets specific direction rather than raw analyzer output.

These rules live in two places — `context.md` in Astro Flux (fed as `.roorules` to every executor call) and `BAD_PATTERNS`/`ERROR_HINTS` in `work.py` (enforced mechanically before code touches disk). Every single one exists because the model burned retries on it repeatedly.

The dynamic time budget tracks a rolling average of recently completed tasks and sets a per-task ceiling at roughly 1.7× that average (capped at 10 minutes). Tasks that exceed the budget are skipped to a retry pass with a 20-minute ceiling. Every outcome — duration, attempt count, error types — is logged to `velocity.jsonl`.


`velocity.py` — **The Dashboard**

Reads `velocity.jsonl` and shows throughput trends: done/failed/total per day, first-attempt success rate, average retries, and top recurring error types. Runs automatically at the start of every standup. The top error types table is the highest-ROI output in the whole system — three occurrences of the same error code is the signal to add a `BAD_PATTERNS` entry or `ERROR_HINTS` mapping rather than keep burning retries.


`poll_status.sh` — **Claude's Eyes**

A read-only script that prints `supervisor.status` + `escalate.md` + the last 30 lines of the supervisor log in one shot. Used by Claude during an active session to poll roughly every 80 seconds. You can also run it manually as a quick status check without opening log files.


**The LLM Layers**

The key design principle is cost tiering. Cheap local models handle routine work; expensive remote models only get called when the local stack is genuinely stuck.

The **4B planner** (qwen3.5:4b) runs first on every task, selecting up to 10 relevant source files. It also powers the advisor loop after each failure, and handles all the standup interactive options — pivot, edit, and fill all go through the 4B model. It runs many times per session, so keeping it small matters.

The **three executor tiers** (gemma4:26b → deepseek-coder-v2:16b → qwen3.5:35b) form a cost cascade. Most tasks resolve at Tier 1. The higher tiers exist for tasks that genuinely need more capability, not as a default.

**Claude and Gemini** sit at the top of the hierarchy, used for two distinct purposes. During execution, Claude monitors the status file and handles escalations — reading `escalate.md`, editing source files, and writing `fixed:N` to resume the loop. Outside of execution, you and Gemini are used for the work that requires full product context: generating the initial vision, designing architecture, reviewing backlog structure, unblocking structural problems the local stack can't reason about. The conversation you've been having here — reviewing the code, fixing compile errors, discussing the system design — is that top layer in action.


**How It Connects to Astro Flux**

The connection points are `ROADMAP.md` (the task queue), `VISION.md` (first 1500 characters fed to every executor call), `context.md` (project-specific API rules and known pitfalls, used as `.roorules`), `LOCKED_FILES` (grows as files are manually verified), `BAD_PATTERNS` (grows from Astro Flux's specific failure history), and `logs/` (velocity data, escalation reports, Claude's polling target).


**The "2 Weeks in a Few Days" Effect**

`plan_week.py` plans at human developer pace — one person, focused work, 22 tasks per day. But `work.py` runs tasks in a tight loop at machine speed with no context-switching or distractions. A task that might take a human developer 20–40 minutes takes the system 2–6 minutes on average. A "2-week" plan compresses to 2–3 days of wall-clock time.

The practical implication: generate 4–6 weeks of backlog upfront rather than 2. Use the standup `f` fill option as a daily top-up rather than your primary backlog management strategy. The system will outpace whatever runway you gave it.


**What Remains to Polish**

Task sizing at generation time is the biggest active friction point. `plan_week.py` enforces sizing rules in its prompt, but the 4B model occasionally lets broad tasks through. These surface as budget timeouts in the execution loop. The cleanest fix is either catching them at standup with `e`, or adding a decomposition step in `work.py` that detects multi-file scope before the first execution attempt and splits automatically.

`.roorules` promotion is semi-manual. The qwen advisor drafts rule suggestions after each failure and logs them, but they aren't automatically promoted into the live rules file. `promote_rules.py` exists for this but isn't wired into the daily loop yet. Closing this loop would make the system genuinely self-improving — each project's failures directly reduce the retry rate on the next one.

Escalation response latency depends on Claude being in an active Cowork session. A push notification when status becomes `needs_fix:N` would let you respond from anywhere rather than needing to check the terminal.

The `nodes.py`/`graph.py` architecture is an earlier iteration of the system (Architect → Executor → Validator → Publisher graph) that predates `work.py`. It still exists in the repo but isn't part of the active daily loop. It could be cleaned up or evolved into a higher-level orchestrator for multi-project coordination.


**Plugging Into Future Projects**

The minimum to onboard a new project is a `ROADMAP.md`, a `VISION.md`, a `.roorules` file (can start empty), and a validator (`flutter analyze`, `pytest`, or `npm test` — auto-detected). Run `init_project.py` to scaffold the `logs/` directory and then `plan_week.py` to fill the backlog.

The `LOCKED_FILES` and `BAD_PATTERNS` sets in `work.py` are currently Astro Flux-specific. For a new project they start empty and grow organically from velocity data. Over multiple projects, the accumulated rules and error hints become a transferable library of project-class knowledge — things that reliably go wrong in Flutter projects, things that reliably go wrong in Python async projects — that makes each subsequent project cheaper to run than the last. The system gets smarter the more you use it.
