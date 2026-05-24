# Sovereign Agent — Claude Context

## Code Environment Labels
Every code snippet must be labelled with one of:
- **[local]** — plain Mac terminal, no venv needed (e.g. git, gcloud, docker commands)
- **[venv]** — terminal with sovereign_agent venv active (`source .venv/bin/activate` or use the `./work` / `./standup` wrappers which activate it automatically)
- **[gcloud ssh: <vm-name>]** — SSH'd into a GCP VM (e.g. `gcloud compute ssh ollama-server --zone us-central1-a`)

---

## Project: Astro Flux

A Flutter/Flame mobile game. Rules: 5 Motes fuse into a Vector, 5 Vectors fuse into a Nova. Stars are capturable nodes that produce Motes in 3 growth tiers. Aesthetic: dark background, high-saturation neon glows, additive blending in combat.

**Tech stack:** Flutter/Flame + GCP (Firestore, Pub/Sub, Cloud Run Jobs, GCE GPU VM)

**Project path:** `~/Code/astro_flux`  
**Sovereign agent path:** `~/Code/Xanadu/sovereign_agent`

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

## Current State (as of 2026-05-21)

- **535 tasks done**, **81 pending** in `task_graph.json` (fresh, generated today, all status=pending)
- `supervisor.status`: `stopped`  
- `escalate.md`: stale from a previous local run — does not block the cloud run
- `tier2_queue.jsonl`: may contain tasks from previous local runs

### Key Prior Session Learnings
- **Do not run 4+ local parallel workers** — local 32B/R1 models (19 GB) starve each other of GPU RAM when loaded concurrently. All tier-3/4 attempts timeout in a loop. The cloud run is the fix (each tier gets its own GPU instance).
- **`AstroGame` constructor** — multiple workers repeatedly hit `1 positional argument expected by 'AstroGame.new', but 0 found` when touching `game_widget.dart`. `lib/game/astro_game.dart` is locked, so models can't see its current signature unless it's fed as context.
- **`burst_animation_component.dart`** — blocked by deprecated API patterns: `.withOpacity()`, wrong `Game.update()` signature, wrong flame import paths. Bad-pattern filters catch these, but the model keeps regenerating them. If it escalates again, manually implement it.
- **`game_widget.dart` is locked** but tasks keep writing to it — investigate whether it should be unlocked or if those tasks should be rerouted.
- **`lib/components/combat_result_label_component.dart`** — referenced by generated code but file doesn't exist. Add to context or create it.
- **Stub detection** — `.roorules` has anti-stub rules. Every task has a `— done when:` acceptance clause. These were added in a prior session.

---

## Cloud Run Startup Sequence

### Prerequisites (check once)
1. `astro_flux` pushed to GitHub (`PROJECT_REPO` env var must be set — **currently missing, no git remote**)
2. Docker image built and pushed to GCR
3. Pub/Sub topics and subscriptions created (`setup_pubsub.sh`)
4. Ollama VM running (`start_ollama_vm.sh` or `gcloud compute instances start ollama-server`)

### Full Launch Sequence

**Step 1 — Push astro_flux to GitHub** [local]
```bash
cd ~/Code/astro_flux
git init && git remote add origin https://github.com/joseantoniolicon/astro_flux.git
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
python orchestrate.py --project ~/Code/astro_flux
```

**Step 6 — Launch tier-1 workers (separate terminal)** [local]
```bash
cd ~/Code/Xanadu/sovereign_agent
PROJECT_REPO=https://github.com/joseantoniolicon/astro_flux.git \
./gcp/run_workers.sh --workers 4
```

**Step 7 — Launch tier-2 worker on the L4 VM** [gcloud ssh: ollama-server]
```bash
docker run --rm \
  -e WORKER_TIER=2 \
  -e OLLAMA_EMBEDDED=false \
  -e OLLAMA_URL=http://localhost:11434 \
  -e ANTHROPIC_API_KEY=<key> \
  -e PROJECT_REPO=https://github.com/joseantoniolicon/astro_flux.git \
  -e FIRESTORE_PROJECT_ID=astro-flux-spyderboy \
  gcr.io/astro-flux-spyderboy/sovereign-worker:latest
```

### Monitoring
```bash
# Status snapshot [venv]
./poll_status.sh

# Orchestrator status [venv]
python orchestrate.py --project ~/Code/astro_flux --status

# Velocity dashboard [venv]
python velocity.py --project ~/Code/astro_flux
```

---

## Wrapper Scripts (handle venv automatically)
These activate the venv internally — no need to activate manually:
- `./work --project ~/Code/astro_flux` → runs `work.py`
- `./standup --project ~/Code/astro_flux` → runs `standup.py`

For `orchestrate.py`, `velocity.py`, `make_graph.py` — either activate the venv first or call them with the venv's Python directly.

---

## Locked Files
Key locked files (cannot be written by workers):  
`lib/game/astro_game.dart`, `lib/game/game_widget.dart`, `lib/game/game_core.dart`,  
`lib/models/*`, `lib/config/*`, `lib/components/mote_component.dart`,  
`lib/components/particle_component.dart`, `pubspec.yaml`, `ROADMAP.md`

Full list in `~/Code/astro_flux/.sovereign_config.json`.
