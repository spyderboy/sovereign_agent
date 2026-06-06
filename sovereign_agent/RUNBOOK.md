# Sovereign Agent — RunPod Runbook

A practical reference for spinning up a GPU sprint. Designed so any AI assistant
can read this and help you run 500 tasks against a project with a single prompt.

---

## Quick start (copy-paste version)

```bash
# 1. Set your API key (one-time per terminal session)
export RUNPOD_API_KEY=rpa_...

# 2. Provision pod + install everything + run sprint
cd ~/Code/Xanadu/sovereign_agent
./runpod/start_ollama_pod.sh --run

# 3. Stop when done (saves money)
./runpod/start_ollama_pod.sh --stop <pod_id>
```

That's it for a standard run. Everything below explains what to do when it
doesn't work, and how to customise model/worker/task choices.

---

## Prerequisites

| What | Where | Notes |
|---|---|---|
| RunPod account | runpod.io | Free to create |
| RunPod API key | runpod.io/console/user/settings → API Keys | Starts with `rpa_` |
| SSH public key | runpod.io/console/user/settings → SSH Public Keys | Must be saved BEFORE pod creation |
| $20+ credits | runpod.io/console/billing | L4 costs ~$0.44/hr |
| GitHub repos | github.com/spyderboy/astro_flux | Must be public |
| GitHub repos | github.com/spyderboy/sovereign_agent | Must be public |

**Generate SSH key if you don't have one:**
```bash
ssh-keygen -t ed25519 -C "tony@astroflux" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub   # copy this entire line into RunPod settings
```

---

## GPU choice

The script tries GPUs in order. Current candidates in `runpod/start_ollama_pod.sh`:

| GPU | VRAM | $/hr | Max workers (14B) | Notes |
|---|---|---|---|---|
| NVIDIA L4 | 24GB | ~$0.44 | 4 | Best availability, reliable |
| RTX A5000 | 24GB | ~$0.16 | 4 | Cheapest 24GB option |
| RTX 3090 | 24GB | ~$0.22 | 4 | Common, good value |
| RTX A6000 | 48GB | ~$0.33 | 6 | More headroom |
| A100 40GB | 40GB | ~$0.79 | 6 | Fastest single-GPU option |

To target only a specific GPU, edit `GPU_CANDIDATES` in `runpod/start_ollama_pod.sh`.

---

## Model choice

Configured via env vars. Current defaults in `start_ollama_pod.sh`:

```
TIER1_MODEL = qwen2.5-coder:14b-instruct-q4_K_M   (~8GB VRAM)
TIER2_MODEL = qwen2.5-coder:14b-instruct-q4_K_M   (same — both passes use 14B)
```

**VRAM budgets on 24GB L4:**

| Config | VRAM used | Max safe workers |
|---|---|---|
| 7B Q4 only | ~5GB + KV cache | 6 workers |
| 14B Q4 only | ~8GB + KV cache | 4 workers |
| 7B quick + 14B deep | ~8GB (sequential) | 4 quick, 2 deep |
| 35B MoE | ~20GB | 1 worker only |

**Rule of thumb:** VRAM = model size + (workers × ~1.5GB KV cache). Stay under 20GB on a 24GB card.

**To override models at runtime:**
```bash
TIER1_MODEL=qwen2.5-coder:7b-instruct-q4_K_M \
TIER2_MODEL=qwen2.5-coder:14b-instruct-q4_K_M \
DEEP_WORKERS=2 \
~/Xanadu/sovereign_agent/supervisor.sh ~/astro_flux --full --workers 4
```

---

## Worker count

`--workers N` controls parallel agents on the quick pass.
`DEEP_WORKERS=N` controls workers on the deep (failure retry) pass.

| Workers | Throughput | Notes |
|---|---|---|
| 1 | Baseline | Good for debugging |
| 2 | ~1.8× | Safe on any GPU |
| 4 | ~3× | Sweet spot for 14B on L4 |
| 6 | ~3.5× | Diminishing returns; risk OOM |

Beyond 4 workers, Ollama serialises inference anyway — workers just queue longer.
The throughput gain from 2→4 is real (file I/O, git, flutter analyze overlap with inference).
The gain from 4→6 is marginal.

---

## Supervisor commands

```bash
# Full run (recommended): quick sweep then deep mop-up
supervisor.sh ~/astro_flux --full --workers 4

# Quick only (tier-1 model): fast sweep, failures queued for later
supervisor.sh ~/astro_flux --quick --workers 4

# Deep only: process queued failures with stronger model
supervisor.sh ~/astro_flux --deep --workers 2

# Sequential (1 worker): safest, useful for debugging
supervisor.sh ~/astro_flux
```

**How `--full` works:**
1. Pass 1 (quick): all unchecked `[ ]` tasks in ROADMAP.md, N workers, tier-1 model
2. Pass 2 (deep): only tasks that failed pass 1, DEEP_WORKERS workers, tier-2 model
3. Tasks marked `[x]` in ROADMAP.md are skipped — safe to interrupt and resume

---

## Adding tasks

Tasks live in `ROADMAP.md` as unchecked `- [ ]` lines. Format:
```
- [ ] <description of what to implement> — done when: <acceptance criteria>
```

**To add a focused batch:**
1. Open a conversation with any AI assistant
2. Share `ROADMAP.md` or describe the codebase
3. Ask: *"Add 500 unchecked tasks focused on [gameplay / AI / visual polish / etc]"*
4. Commit and push ROADMAP.md
5. On the pod: `git pull && supervisor.sh ~/astro_flux --full --workers 4`

**Good task descriptions include:**
- The exact file to edit: `In lib/game/astro_game.dart:`
- What to add/change
- A concrete "done when" test: `done when: flutter analyze 0 errors`

---

## On-pod setup (manual fallback)

If `--run` SSH fails, use the RunPod web terminal
(`runpod.io/console/pods → your pod → Connect → Web Terminal`):

```bash
# Install deps
apt-get update -qq && apt-get install -y -qq zstd pciutils

# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# Start Ollama (background)
OLLAMA_HOST=0.0.0.0:11434 OLLAMA_NUM_PARALLEL=4 OLLAMA_MAX_LOADED_MODELS=2 \
  ollama serve > /tmp/ollama.log 2>&1 &
sleep 8

# Verify GPU detected
grep "inference compute" /tmp/ollama.log

# Pull models
ollama pull qwen2.5-coder:14b-instruct-q4_K_M

# Install Flutter (required for flutter analyze validation)
apt-get install -y curl unzip xz-utils zip libglu1-mesa
cd /opt && curl -sO https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.29.2-stable.tar.xz
tar xf flutter_linux_3.29.2-stable.tar.xz
git config --global --add safe.directory /opt/flutter
ln -sf /opt/flutter/bin/flutter /usr/local/bin/flutter
ln -sf /opt/flutter/bin/dart /usr/local/bin/dart
flutter precache --no-ios --no-android --no-web

# Fix Python symlink (validator uses /usr/bin/python)
ln -sf /usr/bin/python3 /usr/bin/python

# Clone repos
git clone https://github.com/spyderboy/astro_flux.git ~/astro_flux
git clone https://github.com/spyderboy/sovereign_agent.git ~/Xanadu

# Install Python deps
pip install -r ~/Xanadu/sovereign_agent/requirements.txt -q

# Get Flutter deps
cd ~/astro_flux && flutter pub get

# Run sprint
TIER1_MODEL=qwen2.5-coder:14b-instruct-q4_K_M \
TIER2_MODEL=qwen2.5-coder:14b-instruct-q4_K_M \
DEEP_WORKERS=2 \
~/Xanadu/sovereign_agent/supervisor.sh ~/astro_flux --full --workers 4
```

---

## Known gotchas

| Problem | Cause | Fix |
|---|---|---|
| SSH permission denied | SSH key not in RunPod settings before pod creation | Add key, stop pod, start new pod |
| SSH times out | Pod reports RUNNING before SSH daemon starts | Wait 2 min and retry, or use web terminal |
| `No module named pytest` | `/usr/bin/python` is Python 2 | `ln -sf /usr/bin/python3 /usr/bin/python` |
| `flutter: command not found` in workers | Flutter not in PATH for subprocesses | `ln -sf /opt/flutter/bin/flutter /usr/local/bin/flutter` |
| `address already in use` | Previous Ollama still running | `pkill ollama && sleep 3 && ollama serve ...` |
| `ollama: command not found` | Ollama install used `&&` chain with background `&` — chain broke | Run install, serve, pull as separate commands |
| Workers only see 41/N tasks | ROADMAP.md on pod has local changes blocking pull | `git fetch origin && git checkout origin/master -- ROADMAP.md` |
| Git push rejected (large file) | `logs/` or `.dart_tool/` committed | They're in `.gitignore` now; `git rm -r --cached logs/ .dart_tool/` |
| `podRentInterruptable` errors | RunPod deprecated this mutation | Script now uses `podFindAndDeployOnDemand` ✓ |
| GCP A100 quota 0 | New GCP projects have zero GPU quota | Request at console.cloud.google.com/iam-admin/quotas |

---

## Cost estimates

| Scenario | GPU | Workers | Time | Cost |
|---|---|---|---|---|
| 100 tasks, 14B | L4 | 4 | ~25 min | ~$0.18 |
| 500 tasks, 14B | L4 | 4 | ~2 hrs | ~$0.88 |
| 500 tasks, 7B quick + 14B deep | L4 | 4+2 | ~1.5 hrs | ~$0.66 |
| 500 tasks, 14B | RTX A5000 | 4 | ~2 hrs | ~$0.32 |

Model pull time (first run only): 7B ~2 min, 14B ~6 min, 35B ~15 min.
Flutter install (first run only): ~5 min.

---

## Stopping and resuming

```bash
# Stop pod (preserves disk — models cached for next run)
./runpod/start_ollama_pod.sh --stop <pod_id>

# Resume a stopped pod (models already on disk, ~60s to restart)
# Use RunPod console → Pods → Start
# Then SSH in and re-run the supervisor — ROADMAP.md checkboxes track progress

# Check what's left
grep -c "^\- \[ \]" ~/astro_flux/ROADMAP.md
```

Interrupted runs are safe to resume — the supervisor uses ROADMAP.md `[x]` marks
as ground truth. Already-completed tasks are never re-attempted.

---

## Asking an AI to run a sprint

Sample prompt for any AI assistant:

> *"Read RUNBOOK.md in ~/Code/Xanadu/sovereign_agent. I want to run 500 gameplay
> tasks on an L4 instance with 4 workers using the 14B model. Add 500 unchecked
> tasks to ROADMAP.md focused on [topic], push them, then provision the pod and
> start the sprint."*

The AI needs access to:
- This file (RUNBOOK.md)
- `~/Code/Xanadu/sovereign_agent/runpod/start_ollama_pod.sh`
- `~/Code/astro_flux/ROADMAP.md`
- Your `RUNPOD_API_KEY` environment variable
