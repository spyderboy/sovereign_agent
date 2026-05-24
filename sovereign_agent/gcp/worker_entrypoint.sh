#!/usr/bin/env bash
# ── Sovereign Worker Entrypoint ───────────────────────────────────────────────
# Runs inside the Docker container on GCP.
#
# 1. Clones (or pulls) the project repo so workers always use latest code.
# 2. Starts Ollama in the background (if OLLAMA_EMBEDDED=true, the default
#    for Cloud Run Jobs with an attached GPU). Waits until it responds.
#    If OLLAMA_EMBEDDED=false, waits for an external Ollama at OLLAMA_URL
#    instead (legacy persistent-VM mode).
# 3. Pulls the required model if it isn't cached yet.
# 4. Calls worker.py — pulls one task from Pub/Sub, executes it, publishes
#    result back, then exits. Cloud Run Jobs restart the container for the
#    next task automatically.
#
# Environment variables:
#   WORKER_TIER          1 or 2 (default: 1)
#   OLLAMA_EMBEDDED      true  = start Ollama locally (Cloud Run + GPU)
#                        false = use external OLLAMA_URL (persistent VM)
#   OLLAMA_URL           Required when OLLAMA_EMBEDDED=false
#   TIER1_MODEL          7B model name (default: qwen2.5-coder:7b-instruct-q4_K_M)
#   TIER2_MODEL          32B model name (default: qwen2.5-coder:32b)
#   PROJECT_REPO         Git URL of the project to code in (required)
#   PROJECT_BRANCH       Branch to check out (default: main)
#   PROJECT_DIR          Local clone path (default: /app/project)
#   FIRESTORE_PROJECT_ID GCP project for Firestore / Pub/Sub
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_REPO="${PROJECT_REPO:-}"
PROJECT_BRANCH="${PROJECT_BRANCH:-main}"
PROJECT_DIR="${PROJECT_DIR:-/app/project}"
WORKER_TIER="${WORKER_TIER:-1}"
OLLAMA_EMBEDDED="${OLLAMA_EMBEDDED:-true}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
TIER1_MODEL="${TIER1_MODEL:-qwen2.5-coder:7b-instruct-q4_K_M}"
TIER2_MODEL="${TIER2_MODEL:-qwen2.5-coder:32b}"

# ── 1. Clone or update project repo ──────────────────────────────────────────
if [[ -z "$PROJECT_REPO" ]]; then
    echo "ERROR: PROJECT_REPO is not set."
    echo "  Pass -e PROJECT_REPO=https://github.com/you/repo.git"
    exit 1
fi

if [[ -d "$PROJECT_DIR/.git" ]]; then
    echo "→ Pulling latest ($PROJECT_BRANCH)..."
    git -C "$PROJECT_DIR" fetch origin
    git -C "$PROJECT_DIR" checkout "$PROJECT_BRANCH"
    git -C "$PROJECT_DIR" reset --hard "origin/$PROJECT_BRANCH"
else
    echo "→ Cloning $PROJECT_REPO ($PROJECT_BRANCH)..."
    git clone --depth=1 --branch "$PROJECT_BRANCH" "$PROJECT_REPO" "$PROJECT_DIR"
fi

# ── 2. Start / wait for Ollama ────────────────────────────────────────────────
# Tier-3 equivalent (Claude) is handled inline by tier-2 workers — only
# tier 1 and 2 need Ollama.

if [[ "$WORKER_TIER" -lt 3 ]]; then

    if [[ "$OLLAMA_EMBEDDED" == "true" ]]; then
        # ── Embedded mode: start Ollama as a background process ──────────────
        # This is the default for Cloud Run Jobs with an attached GPU.
        # The Ollama binary must be present in the image (see Dockerfile).
        echo "→ Starting embedded Ollama..."
        OLLAMA_HOST="0.0.0.0" ollama serve &
        OLLAMA_PID=$!

        echo "→ Waiting for Ollama to become ready..."
        for i in $(seq 1 40); do
            if curl -sf "${OLLAMA_URL}/api/tags" > /dev/null 2>&1; then
                echo "  ✓ Ollama is up (${i}s)"
                break
            fi
            if [[ $i -eq 40 ]]; then
                echo "ERROR: Ollama did not start after 80s."
                exit 1
            fi
            sleep 2
        done

        # ── Pull the model if not already cached ─────────────────────────────
        if [[ "$WORKER_TIER" -eq 1 ]]; then
            TARGET_MODEL="$TIER1_MODEL"
        else
            TARGET_MODEL="$TIER2_MODEL"
        fi

        echo "→ Ensuring model is available: $TARGET_MODEL"
        if ! ollama list | grep -q "$TARGET_MODEL"; then
            echo "  Pulling $TARGET_MODEL (first run, this may take a few minutes)..."
            ollama pull "$TARGET_MODEL"
        else
            echo "  ✓ Model already cached"
        fi

    else
        # ── External mode: wait for the Ollama GPU VM ─────────────────────────
        if [[ -z "$OLLAMA_URL" ]]; then
            echo "ERROR: OLLAMA_URL is required when OLLAMA_EMBEDDED=false."
            echo "  Pass -e OLLAMA_URL=http://<gpu-vm-internal-ip>:11434"
            exit 1
        fi
        echo "→ Waiting for external Ollama at $OLLAMA_URL..."
        for i in $(seq 1 30); do
            if curl -sf "${OLLAMA_URL}/api/tags" > /dev/null 2>&1; then
                echo "  ✓ Ollama is up"
                break
            fi
            if [[ $i -eq 30 ]]; then
                echo "ERROR: Ollama at $OLLAMA_URL did not respond after 60s."
                exit 1
            fi
            sleep 2
        done
    fi
fi

# ── 3. Run the worker ─────────────────────────────────────────────────────────
echo "→ Starting tier-$WORKER_TIER worker"
exec python /app/sovereign_agent/worker.py \
    --project "$PROJECT_DIR" \
    --tier "$WORKER_TIER"
