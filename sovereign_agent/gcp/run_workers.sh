#!/usr/bin/env bash
# ── Launch Sovereign Agent workers on GCP ────────────────────────────────────
#
# Reads task_graph.json from the project repo to find dependency layers,
# then fires N parallel Docker containers (workers) per layer.
# Workers within a layer run in parallel; the next layer only starts once
# all workers in the current layer finish.
#
# Prerequisites:
#   • Docker installed and logged in to Artifact Registry (or use --local)
#   • Ollama GPU VM running (see gcp/start_ollama_vm.sh)
#   • ANTHROPIC_API_KEY and FIRESTORE_PROJECT_ID set in env or .env file
#
# Usage:
#   # Run 2 parallel workers against astro_flux:
#   OLLAMA_URL=http://10.128.0.5:11434 \
#   ANTHROPIC_API_KEY=sk-ant-... \
#   FIRESTORE_PROJECT_ID=astro-flux-spyderboy \
#   PROJECT_REPO=https://github.com/you/astro_flux.git \
#   ./gcp/run_workers.sh --workers 2
#
#   # Dry run — print what would launch without actually running:
#   ./gcp/run_workers.sh --workers 2 --dry-run
#
#   # Quick pass only (tier 1, queue failures for --deep):
#   ./gcp/run_workers.sh --workers 2 --quick
#
#   # Deep pass over whatever --quick left in the queue:
#   ./gcp/run_workers.sh --workers 2 --deep
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
WORKERS="${WORKERS:-1}"
IMAGE="${SOVEREIGN_IMAGE:-sovereign-worker:latest}"
PROJECT_REPO="${PROJECT_REPO:-}"
PROJECT_BRANCH="${PROJECT_BRANCH:-main}"
OLLAMA_URL="${OLLAMA_URL:-}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
FIRESTORE_PROJECT_ID="${FIRESTORE_PROJECT_ID:-}"
DRY_RUN=false
WORK_FLAGS=""         # extra flags passed through to work.py (e.g. --quick, --deep)

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers)    WORKERS="$2"; shift 2 ;;
        --image)      IMAGE="$2";   shift 2 ;;
        --dry-run)    DRY_RUN=true; shift   ;;
        --quick)      WORK_FLAGS="$WORK_FLAGS --quick"; shift ;;
        --deep)       WORK_FLAGS="$WORK_FLAGS --deep";  shift ;;
        --max-tier)   WORK_FLAGS="$WORK_FLAGS --max-tier $2"; shift 2 ;;
        *)            echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── Validate required env ─────────────────────────────────────────────────────
missing=()
[[ -z "$PROJECT_REPO"        ]] && missing+=("PROJECT_REPO")
[[ -z "$OLLAMA_URL"          ]] && missing+=("OLLAMA_URL")
[[ -z "$ANTHROPIC_API_KEY"   ]] && missing+=("ANTHROPIC_API_KEY")
[[ -z "$FIRESTORE_PROJECT_ID" ]] && missing+=("FIRESTORE_PROJECT_ID")

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: Missing required environment variables:"
    for v in "${missing[@]}"; do echo "  $v"; done
    echo ""
    echo "Example:"
    echo "  export PROJECT_REPO=https://github.com/you/astro_flux.git"
    echo "  export OLLAMA_URL=http://10.128.0.5:11434"
    echo "  export ANTHROPIC_API_KEY=sk-ant-..."
    echo "  export FIRESTORE_PROJECT_ID=astro-flux-spyderboy"
    exit 1
fi

echo "═══════════════════════════════════════════════════"
echo "  Sovereign Agent — Worker Launcher"
echo "  Workers    : $WORKERS"
echo "  Image      : $IMAGE"
echo "  Ollama     : $OLLAMA_URL"
echo "  Firestore  : $FIRESTORE_PROJECT_ID"
echo "  Repo       : $PROJECT_REPO ($PROJECT_BRANCH)"
echo "  Flags      : ${WORK_FLAGS:-none}"
echo "  Dry run    : $DRY_RUN"
echo "═══════════════════════════════════════════════════"

# ── Clean up any leftover containers from previous runs ──────────────────────
for (( i=0; i<WORKERS; i++ )); do
    docker rm -f "sovereign-worker-${i}" > /dev/null 2>&1 || true
done

if [[ "$DRY_RUN" == "true" ]]; then
    echo ""
    for (( i=0; i<WORKERS; i++ )); do
        echo "  [DRY RUN] docker run --rm --name sovereign-worker-${i} ... $IMAGE $WORK_FLAGS"
    done
    echo ""
    echo "Dry run complete — no containers started."
    exit 0
fi

# ── Launch all workers in parallel and wait ───────────────────────────────────
echo ""
echo "→ Launching $WORKERS worker(s)..."

pids=()
for (( i=0; i<WORKERS; i++ )); do
    echo "  → Launching sovereign-worker-${i} (worker $i / $WORKERS)..."
    docker run --rm \
        --name "sovereign-worker-${i}" \
        -e "OLLAMA_URL=$OLLAMA_URL" \
        -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
        -e "FIRESTORE_PROJECT_ID=$FIRESTORE_PROJECT_ID" \
        -e "PROJECT_REPO=$PROJECT_REPO" \
        -e "PROJECT_BRANCH=$PROJECT_BRANCH" \
        -e "WORKER_ID=$i" \
        -e "STRIDE=$WORKERS" \
        -e "TIER1_MODEL=${TIER1_MODEL:-qwen2.5-coder:7b-instruct-q4_K_M}" \
        -e "TIER2_MODEL=${TIER2_MODEL:-qwen2.5-coder:32b}" \
        -e "CLAUDE_MODEL=${CLAUDE_MODEL:-claude-sonnet-4-6}" \
        "$IMAGE" \
        $WORK_FLAGS &
    pids+=($!)
done

# ── Wait for all workers and collect exit codes ───────────────────────────────
echo ""
echo "→ All $WORKERS workers running. Waiting for completion..."
failed=0
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        echo "  ⚠  Worker PID $pid exited with error"
        ((failed++)) || true
    fi
done

echo ""
if [[ $failed -eq 0 ]]; then
    echo "✓ All workers completed successfully."
else
    echo "⚠  $failed worker(s) failed. Check logs above."
    exit 1
fi
