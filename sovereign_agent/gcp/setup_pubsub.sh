#!/usr/bin/env bash
# ── Sovereign Agent — Pub/Sub bootstrap ───────────────────────────────────────
# Run once before starting a sprint. Creates the 4 topics and 4 subscriptions
# that wire the orchestrator to the three worker tiers.
#
# Safe to re-run — existing topics/subscriptions are left untouched.
#
# Usage:
#   GCP_PROJECT=astro-flux-spyderboy ./gcp/setup_pubsub.sh
#
# Prerequisites:
#   gcloud auth login
#   gcloud config set project <your-project>
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"

if [[ -z "$PROJECT" ]]; then
    echo "ERROR: Set GCP_PROJECT or run: gcloud config set project <id>"
    exit 1
fi

echo "═══════════════════════════════════════════════════"
echo "  Sovereign Agent — Pub/Sub setup"
echo "  Project: $PROJECT"
echo "═══════════════════════════════════════════════════"

# ── Helper ────────────────────────────────────────────────────────────────────
create_topic() {
    local name="$1"
    if gcloud pubsub topics describe "$name" --project="$PROJECT" \
           > /dev/null 2>&1; then
        echo "  ✓ topic $name (already exists)"
    else
        gcloud pubsub topics create "$name" --project="$PROJECT"
        echo "  + topic $name created"
    fi
}

create_sub() {
    local sub="$1" topic="$2"
    local extra="${3:-}"
    if gcloud pubsub subscriptions describe "$sub" --project="$PROJECT" \
           > /dev/null 2>&1; then
        echo "  ✓ sub   $sub (already exists)"
    else
        # shellcheck disable=SC2086
        gcloud pubsub subscriptions create "$sub" \
            --topic="$topic" \
            --project="$PROJECT" \
            --ack-deadline=600 \
            --message-retention-duration=1d \
            $extra
        echo "  + sub   $sub created"
    fi
}

# ── Topics ────────────────────────────────────────────────────────────────────
# Note: no tasks-claude topic — Claude escalation is handled inline by
# tier-2 workers (32B fails → same worker calls Claude, then reports back).
echo ""
echo "→ Topics"
create_topic "tasks-tier1"   # orchestrator → 7B workers
create_topic "tasks-tier2"   # orchestrator → 32B+Claude workers
create_topic "task-results"  # all workers  → orchestrator

# ── Subscriptions ─────────────────────────────────────────────────────────────
# Each tier has its own subscription so multiple workers can compete for tasks
# (Pub/Sub delivers each message to exactly one subscriber in a group).
# The orchestrator has a separate subscription on task-results.
echo ""
echo "→ Subscriptions"
create_sub "tasks-tier1-worker"        "tasks-tier1"
create_sub "tasks-tier2-worker"        "tasks-tier2"
create_sub "task-results-orchestrator" "task-results"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✓ Pub/Sub ready. Start the sprint:"
echo ""
echo "  1. python make_graph.py --project <project>"
echo "  2. python orchestrate.py --project <project>"
echo "  3. docker run -e WORKER_TIER=1 ... sovereign-worker  # 7B, repeat N times"
echo "  4. docker run -e WORKER_TIER=2 ... sovereign-worker  # 32B+Claude inline, repeat M times"
echo "═══════════════════════════════════════════════════"
