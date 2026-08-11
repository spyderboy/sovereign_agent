# Sovereign Agent — Architecture Audit (Documented vs. Actual)

Compiled 2026-07-09 by reading every core script (`plan_week.py`, `standup.py`, `work.py`, `worker.py`, `orchestrate.py`, `qwen_advisor.py`, `promote_rules.py`, `supervisor.sh`), the docs (`OVERVIEW.md`, `RUNBOOK.md`, `sovereign_agent/CLAUDE.md`), and `.env`/`.sovereign_config.json` for the Galaxican project. Every claim below is sourced to a specific file/line, not inferred from the docs alone.

There are **two entirely separate execution engines** in this repo, not one system with two modes. That's the single most important thing to understand before anything else makes sense.

| | **Local engine** (what you've actually been running) | **Cloud engine** (what the docs mostly describe) |
|---|---|---|
| Entry point | `supervisor.sh` → `work.py` | `orchestrate.py` → Pub/Sub → `worker.py` |
| Where it runs | Your Mac, one task at a time (or N parallel workers by stride) | Cloud Run Jobs (tier 1) + a GCE L4 GPU VM (tier 2), dispatched via Google Pub/Sub |
| State tracking | `logs/*.jsonl`, `ROADMAP.md` checkboxes | Firestore (`sprint_tasks` collection) |
| Status as of this session | Fully working — this is what raced gemma4 vs qwen2.5-coder tonight | **Not operational.** `sovereign_agent/CLAUDE.md`'s own "Current State" note (dated 2026-05-21, itself stale) says the prerequisite step — pushing `galaxican` to GitHub so cloud workers can pull the code — was never done ("`PROJECT_REPO` env var must be set — currently missing, no git remote"). Nothing in tonight's `.env` or session suggests that changed. |

Everything from here is organized as: **what it says** → **what it does**.

---

## 1. Concept generation & refinement

**What it says** (`OVERVIEW.md`): `plan_week.py` is "the AI Product Manager" — reads `VISION.md`, generates a full `ROADMAP.md`. `standup.py` is the daily interactive steering interface.

**What it does:**
- `plan_week.py` reads `VISION.md` + the existing `ROADMAP.md` (so it doesn't repeat tasks), then calls a single Ollama model (`PLANNER_MODEL`) once per week of backlog, asking for strict JSON: N days × `--tasks-per-day` tasks, each required to touch 1–2 files and end in a `— done when:` clause. This part matches the docs closely — the sizing-rule enforcement in the prompt (plan_week.py:94–113) is real and detailed.
- Each week is planned with the *previous* week's output appended as context (`rolling_roadmap`, plan_week.py:198), so week 2 doesn't repeat week 1 — matches OVERVIEW.md's claim.
- `standup.py` I didn't fully re-verify line-by-line this session, but its file exists and matches the described five-option menu (y/f/p/e/q) from earlier reading of OVERVIEW.md; no contradicting evidence found.

**Drift found:** `PLANNER_MODEL` defaults to `qwen2.5-coder:7b-instruct-q4_K_M` (work.py:98, plan_week.py:21) — **not** the "4B model" that `OVERVIEW.md` repeatedly credits with planning ("That one command generates 220 tasks... all from the local 4B model"). There is no 4B model configured anywhere in `.env` or the code defaults. This may be a leftover description from an earlier version of the pipeline.

---

## 2. Backlog creation

**What it says**: dates in `ROADMAP.md` are cosmetic; `work.py` reads all unchecked items in order.

**What it does:** Confirmed accurate — `plan_week.py` writes everything under a single date header (plan_week.py:200-207: "All tasks go under a single date... Date headers are for human readability only"), and `work.py` just walks the checkbox list. This part of the docs is correct.

---

## 3. Orchestration

**What it says** (`sovereign_agent/CLAUDE.md`): a full cloud pipeline — `orchestrate.py` reads `task_graph.json`, tracks state in Firestore, publishes to Pub/Sub topics `tasks-tier1`/`tasks-tier2`, workers on Cloud Run Jobs and a GCE L4 VM pull tasks, push results back, Claude escalates inline on tier-2 failure.

**What actually runs today:** `supervisor.sh`, a bash loop that calls `work.py` directly (no Pub/Sub, no Firestore task state — Firestore is used for something else entirely, see §6). This is the *only* path exercised in this session.

**Confirmed-broken pieces of the cloud path:**
- **Different tier count.** `orchestrate.py`'s own docstring says "two-tier pipeline" and only defines `TOPIC_TIER1`/`TOPIC_TIER2` constants (orchestrate.py:6-9, 53-55) — no tier-3/tier-4 topics exist. But `worker.py` was upgraded to the current 4-tier model set (worker.py:8-13, tier_desc dict at :336-341) and has full `run_tier3`/`run_tier4` implementations. Since `orchestrate.py` never publishes to a tier-3 or tier-4 topic, those code paths in `worker.py` are currently unreachable from the orchestrator — they'd only ever run if invoked manually (`WORKER_TIER=3 python worker.py ...`, worker.py:21-24).
- **Missing prerequisite.** `sovereign_agent/CLAUDE.md`:98 explicitly flags `PROJECT_REPO` (the git remote cloud workers pull from) as unset. Without it, Cloud Run Job workers have no way to get the Galaxican source.
- **Stale status snapshot.** The same doc's "Current State" section is dated 2026-05-21 — over six weeks stale relative to today — and says `supervisor.status: stopped`.

Net effect: the sophisticated multi-machine architecture in the docs (Mac + PC/WSL + GPU VM, per the "Machine Map" table) is a real, partially-built system, but not the one doing any work right now. Everything tonight ran through the simpler local loop.

---

## 4. Worker tiers (the part that's actually running)

**What it says vs. does — this one matches**, with the caveat that `.env` was overriding it until tonight's fix. Current live config (`work.py`:57-62, confirmed via the corrected `.env`):

| Tier | Model | Size | Role |
|---|---|---|---|
| 1 | `qwen2.5-coder:7b-instruct-q4_K_M` | 7B dense | Handles the majority of tasks |
| 2 | `gemma4:26b` | 26B MoE, ~4B active | Quick second opinion |
| 3 | `qwen3.6:35b-a3b` | 35B MoE, ~3B active | Third opinion |
| 4 | `qwen2.5-coder:32b` | 32B dense | Heavy hitter before Claude |
| — | Claude (`claude-sonnet-4-6`) | — | Final escalation, **disabled by default** (`CLAUDE_ENABLED=0`, work.py:103) |

Escalation mechanics (work.py:126-146): a tier advances to the next model after **2 identical errors** (`PHASE_STRIKE_LIMIT`). The attempt ceiling per tier is NOT uniform — it never was correctly displayed as one, either: `PHASE_MAX_ATTEMPTS` (tier 1) = **3** (lowered from 6 on 2026-07-10), `TIER2_MAX_ATTEMPTS` (tiers 2-3) = **2**, `TIER4_MAX_ATTEMPTS` (tier 4, the last automated resort) = **2**, each independently tunable via `_max_attempts_for_tier()`. Before 2026-07-10 the printed "Attempt X/6" label was wrong for every tier except tier 1 — it always displayed the tier-1 constant regardless of which ceiling was actually enforced (tiers 2-4 were really capped at 2 the whole time, not 6). Worst-case total attempts across all 4 tiers before a task needs human attention: 3 + 2 + 2 + 2 = 9 (was up to 10 before the tier-1 change, and looked like up to 24 from the misleading label).

**Drift found:** `--quick` mode's own help text (until tonight) claimed "tier-1 only." The actual code (`run_start_tier_idx = 1 if args.quick else 0`, work.py:95) **skips tier 1 and starts at tier 2** — this is the bug we hit live tonight and just fixed the comment for. `--quick` currently means "tier 2 only," since tier 3 (35B) exceeds the `<30B` quick-mode parameter cap.

**A/B racing** (work.py:1479-1521, `RACE_MODEL`/`RACE_ENABLED`): only fires when `tier_idx == 0` — i.e., only on a plain run, never under `--quick` or `--deep`. Tonight's `.env` change (`RACE_MODEL=gemma4:12b-mlx`, `RACE_ENABLED=1`) is now correctly wired to fire on the plain `./supervisor.sh ~/Code/galaxican` run in progress.

---

## 5. Qwen advisor

**Correction (2026-07-10):** `qwen_advisor.py`'s docstring used to claim "Uses qwen3.5:4b-nvfp4," which was drift, not design — no 4B model was ever configured anywhere. The docstring now correctly states the real default: `ADVISOR_MODEL` = `qwen2.5-coder:7b-instruct-q4_K_M`, the same weights as `TIER1_MODEL` (qwen_advisor.py:27, work.py:99). That's deliberate: an advisor call during a Tier 1 retry costs nothing extra since the model is already resident in VRAM.

The real problem this drift was masking: any advisor call during a Tier 2-4 retry evicts that tier's much larger model (gemma4:26b / qwen3.6:35b / qwen2.5-coder:32b) from GPU and forces a ~20-30s reload afterward, and `--no-advisor` (the flag meant to prevent this) was never auto-applied anywhere. As of 2026-07-10, `work.py`'s `run_task()` only calls the advisor when `tier_idx == 0` (Tier 1) — Tier 2-4 retries skip it unconditionally now, regardless of `--no-advisor`.

What the advisor actually does, confirmed accurate against its own description: runs *after* `autofix.py`'s mechanical fixes, only on genuinely novel errors. Two real, working mechanisms:
1. **Deterministic import-hint bypass** (`undefined_class_hint`, qwen_advisor.py:80-113): before even asking the LLM, it regexes the analyzer output for "undefined class X," greps `lib/**/*.dart` for where `X` is actually defined, and hands back a precise `import` directive — no model call needed for the single most common error class.
2. **LLM classification** for everything else: classifies error category, drafts an `enriched_hint` for the next attempt, and optionally proposes a `new_rule` for `.roorules`.

**Drift found (cosmetic, not functional):** `_find_class_definition`'s fallback package name (qwen_advisor.py:63) is hardcoded to `"astro_flux"` — stale from before the rename to Galaxican. It's only used if `pubspec.yaml` can't be read, which won't happen in practice, so this is low-severity but worth fixing in the analyze-cleanup pass.

---

## 6. Learning loop — three separate mechanisms, not one

This is the part where the docs undersell what's actually there, in one direction, and oversell it in another.

1. **`.roorules` promotion** (`qwen_advisor.py` → `logs/rule_drafts.jsonl` → `promote_rules.py` → `.roorules`). Every novel error gets logged; a rule proposed 2+ times (threshold set in `supervisor.sh`) gets reviewed by qwen and appended to `.roorules` under `## Learned Rules`. **Correction (2026-07-10): this IS automatic.** `supervisor.sh`'s `run_promote_rules()` (lines 184-188) calls `promote_rules.py --threshold 2` after every completed batch, in both the sequential path (line 341) and the parallel-workers path (line 260) — so it fires incrementally throughout a long unattended run, not just at the end. The original claim above (no call site) was a research error in this audit, not a real gap — `OVERVIEW.md`'s "isn't wired into the daily loop yet" note is itself stale and should be corrected too.

2. **Firestore lessons sync** (`work.py:1090` `_firestore_push_lessons`, `work.py:1178` `_firestore_pull_lessons`) — **this one is real and automatic**, and isn't mentioned in `OVERVIEW.md` at all. Every `work.py` run — local or cloud — pulls remote error patterns and rule drafts from Firestore at startup ("↕ Pulling lessons from Firestore..." — the line you saw in tonight's terminal output) and pushes local learnings back, using `merge=True` so concurrent workers don't clobber each other. This is how a local Mac run and a hypothetical cloud run would share learning, if the cloud side were active. Tonight's runs pulled "0 new records" simply because nothing else has pushed anything yet.

3. **BAD_PATTERNS / ERROR_HINTS** (in `work.py`, mechanical, zero-cost) — pre-write regex rejection of known-wrong code patterns, and post-failure hint injection keyed to specific `flutter analyze` error codes. This is the fastest, cheapest layer and matches `OVERVIEW.md`'s description accurately.

---

## 7. Tier escalation — the actual state machine

Per task, per tier, in order:

1. Try the tier's model. On failure, `autofix.py` applies zero-cost mechanical fixes first.
2. If still failing **and this is a Tier 1 attempt**, `qwen_advisor.advise()` classifies the error and enriches the retry prompt (skipped on Tier 2-4 since 2026-07-10 — see §5).
3. Retry with the enriched prompt.
4. If the **same error signature** repeats twice (`PHASE_STRIKE_LIMIT=2`), advance to the next tier.
5. If a tier exhausts 6 total attempts (`PHASE_MAX_ATTEMPTS=6`) without advancing, the task is either escalated further or, in a capped run (`--quick`/`--max-tier`), written to `logs/tier2_queue.jsonl` for a later `--deep` pass.
6. After all configured tiers are exhausted, and only if `CLAUDE_ENABLED=1` (off by default), Claude is called as the final tier.

This matches the documented cascade concept closely — the main correction from tonight is which tier a given CLI flag actually *starts* you at (§4).

---

## Summary: what to trust

- **Trust**: the local `supervisor.sh` → `work.py` loop, the 4-tier model cascade, `PHASE_STRIKE_LIMIT`/`PHASE_MAX_ATTEMPTS` mechanics, `plan_week.py`'s backlog generation, BAD_PATTERNS/ERROR_HINTS, and the Firestore lessons sync. All verified working tonight.
- **Don't trust without checking first**: any doc mentioning a "4B" planner model (it's 7B — the advisor's "4B" claim was fixed 2026-07-10), `--quick`'s scope (it skips tier 1), or the cloud/Pub-Sub architecture being live (it isn't — that whole path was archived 2026-07-10; see `sovereign_agent/archive/README.md`).
- **Fixed since this audit was written**: `.roorules` promotion is automatic (see §6 correction above); `RACE_MODEL` gets a real context window instead of silently truncating (was falling through to a 16384 default, now 65536); the qwen advisor no longer evicts Tier 2-4 models from GPU (see §5 correction above); parallel workers no longer split dependent tasks (same-file runs and implement→test pairs are chain-assigned to a single worker instead of being scattered by raw stride).
