#!/usr/bin/env bash
# ── Sovereign Agent — Local Runner ──────────────────────────────────────────
# Runs N tier-1 workers + 1 tier-2 worker locally.
# Tier progression:  7B  →  14B (or 7B if LOW_RAM=1)  →  Claude
#
# Usage:
#   ./run_local.sh                  # 2 tier-1 workers + 1 tier-2 worker
#   WORKERS=1 ./run_local.sh        # 1 tier-1 worker (gentle on RAM)
#   LOW_RAM=1 ./run_local.sh        # skips tier-2 model pull (8GB Air)
#
# Requires:
#   - Ollama running:  ollama serve
#   - .venv activated and .env loaded before calling this script
#   - python worker.py accessible in the current venv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKERS="${WORKERS:-3}"
LOW_RAM="${LOW_RAM:-0}"
PROJECT="${PROJECT:-$HOME/Code/astro_flux}"

TIER1_MODEL="${TIER1_MODEL:-qwen2.5-coder:7b-instruct-q4_K_M}"
TIER2_MODEL="${TIER2_MODEL:-qwen2.5-coder:32b}"

echo "═══════════════════════════════════════════════════════"
echo "  Sovereign Agent — Local Runner"
echo "  Tier-1 workers : $WORKERS  (model: $TIER1_MODEL)"
if [[ "$LOW_RAM" == "1" ]]; then
  echo "  Tier-2         : SKIPPED (LOW_RAM=1 — failed tasks → Claude)"
else
  echo "  Tier-2 worker  : 1  (model: $TIER2_MODEL → Claude fallback)"
fi
echo "  Project        : $PROJECT"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── Check Ollama is reachable ─────────────────────────────────────────────────
if ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
  echo "ERROR: Ollama is not running on localhost:11434"
  echo "  Start it with:  ollama serve"
  exit 1
fi
echo "✓ Ollama is running"

# ── Pull required models ──────────────────────────────────────────────────────
echo ""
echo "→ Pulling models (skips if already cached)..."
ollama pull "$TIER1_MODEL"

if [[ "$LOW_RAM" != "1" ]]; then
  ollama pull "$TIER2_MODEL"
fi
echo "✓ Models ready"

# ── Worker loop function ──────────────────────────────────────────────────────
run_worker_loop() {
  local tier="$1"
  local label="tier${tier}"
  echo "  [${label}] worker loop starting..."
  while true; do
    WORKER_TIER="$tier" \
    TIER2_MODEL="$TIER2_MODEL" \
    python "$SCRIPT_DIR/worker.py" --project "$PROJECT" || true
    # Small pause between tasks to avoid hammering Ollama on failure
    sleep 2
  done
}

# ── Launch workers ────────────────────────────────────────────────────────────
echo ""
echo "→ Launching workers (Ctrl-C to stop all)..."

PIDS=()

for i in $(seq 1 "$WORKERS"); do
  run_worker_loop 1 &
  PIDS+=($!)
  echo "  [tier1] worker $i started (pid $!)"
done

if [[ "$LOW_RAM" != "1" ]]; then
  run_worker_loop 2 &
  PIDS+=($!)
  echo "  [tier2] worker started (pid $!)"
fi

# ── Graceful shutdown on Ctrl-C ───────────────────────────────────────────────
cleanup() {
  echo ""
  echo "→ Stopping all workers..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null
  echo "✓ All workers stopped."
}
trap cleanup SIGINT SIGTERM

echo ""
echo "Workers running. Ctrl-C to stop."
wait
