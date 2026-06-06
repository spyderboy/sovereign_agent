#!/usr/bin/env bash
# ── Sovereign Agent — RunPod GPU pod provisioner ──────────────────────────────
#
# Creates a RunPod pod with Ollama + your models, then optionally SSHes in
# and runs the full sovereign sprint automatically.
#
# Prerequisites:
#   1. Create a RunPod account at runpod.io
#   2. Add your SSH public key at runpod.io/console/user/settings (SSH Keys tab)
#   3. Generate an API key at runpod.io/console/user/settings (API Keys tab)
#   4. Export it: export RUNPOD_API_KEY=your_key_here
#      Or add to ~/.zshrc: export RUNPOD_API_KEY=...
#
# Usage:
#   ./runpod/start_ollama_pod.sh                    # provision pod, print SSH cmd
#   ./runpod/start_ollama_pod.sh --run              # provision + SSH in + full sprint
#   ./runpod/start_ollama_pod.sh --stop POD_ID      # stop a running pod
#   ./runpod/start_ollama_pod.sh --status POD_ID    # check pod status
#
# Estimated cost:
#   L4  24GB interruptible: ~$0.14/hr  — fits both models, 4 workers
#   A100 40GB interruptible: ~$0.79/hr — more headroom
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; RESET="\033[0m"

# ── Config ────────────────────────────────────────────────────────────────────
RUNPOD_API_KEY="${RUNPOD_API_KEY:-}"
POD_NAME="ollama-sovereign"
CONTAINER_IMAGE="runpod/pytorch:2.2.0-py3.10-cuda12.1.1-devel-ubuntu22.04"
VOLUME_GB=100        # persistent — models cached here; survives pod stop
CONTAINER_DISK_GB=50
MIN_VCPU=4
MIN_RAM_GB=20
TIER1_MODEL="${TIER1_MODEL:-qwen2.5-coder:14b-instruct-q4_K_M}"
TIER2_MODEL="${TIER2_MODEL:-qwen2.5-coder:14b-instruct-q4_K_M}"
QUICK_WORKERS="${QUICK_WORKERS:-2}"   # 2 × 14B Q4 ~8GB = 16GB — safe on 24GB
DEEP_WORKERS="${DEEP_WORKERS:-2}"     # same model for both passes
ASTRO_REPO="${ASTRO_REPO:-https://github.com/spyderboy/astro_flux.git}"
XANADU_REPO="${XANADU_REPO:-https://github.com/spyderboy/Xanadu.git}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

# GPU candidates — L4 only; add others here if you want fallbacks
GPU_CANDIDATES=(
    "NVIDIA L4|L4 24GB ~\$0.44/hr"
)

# ── Helpers ───────────────────────────────────────────────────────────────────
# API key goes in URL query param, not Bearer header (RunPod's current scheme)
gql() {
    local query="$1"
    curl -s \
        -H "Content-Type: application/json" \
        -X POST "https://api.runpod.io/graphql?api_key=${RUNPOD_API_KEY}" \
        -d "{\"query\": $(echo "$query" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
}

check_deps() {
    for cmd in curl jq python3; do
        command -v "$cmd" &>/dev/null || {
            echo -e "${RED}Error: $cmd is required but not installed.${RESET}"
            exit 1
        }
    done
}

validate_key() {
    if [[ -z "$RUNPOD_API_KEY" ]]; then
        echo -e "${RED}Error: RUNPOD_API_KEY is not set.${RESET}"
        echo "  Get one at: https://runpod.io/console/user/settings (API Keys tab)"
        echo "  Then: export RUNPOD_API_KEY=your_key"
        exit 1
    fi
}

# ── Sub-commands ──────────────────────────────────────────────────────────────
stop_pod() {
    local pod_id="$1"
    echo "→ Stopping pod $pod_id..."
    gql "mutation { podStop(input: { podId: \"$pod_id\" }) { id desiredStatus } }" \
        | jq -r '.data.podStop | "  Pod \(.id) → \(.desiredStatus)"'
}

pod_status() {
    local pod_id="$1"
    gql "query { pod(input: { podId: \"$pod_id\" }) {
            id name desiredStatus runtime {
                ports { ip isIpPublic privatePort publicPort type }
            }
        }
    }" | jq -r '.data.pod |
        "  ID:     \(.id)\n  Name:   \(.name)\n  Status: \(.desiredStatus)\n  Ports:  \(.runtime.ports // [] | map("\(.ip):\(.publicPort) → \(.privatePort)/\(.type)") | join(", "))"'
}

# ── Parse args ────────────────────────────────────────────────────────────────
AUTO_RUN=false
case "${1:-}" in
    --stop)
        validate_key
        stop_pod "${2:?--stop requires a pod ID}"
        exit 0
        ;;
    --status)
        validate_key
        pod_status "${2:?--status requires a pod ID}"
        exit 0
        ;;
    --run)
        AUTO_RUN=true
        ;;
    --help|-h)
        sed -n '2,20p' "$0" | grep '^#' | sed 's/^# \?//'
        exit 0
        ;;
esac

check_deps
validate_key

echo -e "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  🤖  Sovereign Agent — RunPod provisioner${RESET}"
echo -e "${BOLD}  Trying GPU candidates in order...${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"

# ── Try each GPU candidate ────────────────────────────────────────────────────
POD_ID=""
for candidate in "${GPU_CANDIDATES[@]}"; do
    IFS='|' read -r gpu_type cost_note <<< "$candidate"
    echo "→ Trying $cost_note..."

    RESPONSE=$(gql "mutation {
        podFindAndDeployOnDemand(input: {
            cloudType: ALL
            gpuCount: 1
            volumeInGb: $VOLUME_GB
            containerDiskInGb: $CONTAINER_DISK_GB
            minVcpuCount: $MIN_VCPU
            minMemoryInGb: $MIN_RAM_GB
            gpuTypeId: \"$gpu_type\"
            name: \"$POD_NAME\"
            imageName: \"$CONTAINER_IMAGE\"
            dockerArgs: \"\"
            ports: \"22/tcp\"
            volumeMountPath: \"/workspace\"
            env: [
                { key: \"OLLAMA_HOST\",             value: \"0.0.0.0:11434\" },
                { key: \"OLLAMA_NUM_PARALLEL\",      value: \"$QUICK_WORKERS\" },
                { key: \"OLLAMA_MAX_LOADED_MODELS\", value: \"2\" }
            ]
        })
        { id imageName machineId machine { podHostId } }
    }")

    if echo "$RESPONSE" | jq -e '.data.podFindAndDeployOnDemand.id' &>/dev/null; then
        POD_ID=$(echo "$RESPONSE" | jq -r '.data.podFindAndDeployOnDemand.id')
        echo -e "${GREEN}✓ Pod created: $POD_ID  ($cost_note)${RESET}"
        break
    else
        ERROR=$(echo "$RESPONSE" | jq -r '.errors[0].message // .data.podFindAndDeployOnDemand // "unavailable"' 2>/dev/null)
        echo "  ✗ $ERROR — trying next..."
    fi
done

if [[ -z "$POD_ID" ]]; then
    echo -e "${RED}All GPU options unavailable. Try again later or check runpod.io/console.${RESET}"
    exit 1
fi

# ── Wait for pod to be running ────────────────────────────────────────────────
echo ""
echo "→ Waiting for pod to start (this takes 1–3 min)..."
SSH_IP=""
SSH_PORT=""
MAX_WAIT=180
WAITED=0

while [[ $WAITED -lt $MAX_WAIT ]]; do
    STATUS=$(gql "query { pod(input: { podId: \"$POD_ID\" }) {
        desiredStatus runtime {
            ports { ip isIpPublic privatePort publicPort type }
        }
    }}")

    DESIRED=$(echo "$STATUS" | jq -r '.data.pod.desiredStatus // "UNKNOWN"')

    if [[ "$DESIRED" == "RUNNING" ]]; then
        SSH_PORT=$(echo "$STATUS" | jq -r '.data.pod.runtime.ports[]? | select(.privatePort==22 and .isIpPublic==true) | .publicPort' 2>/dev/null | head -1)
        SSH_IP=$(echo "$STATUS"   | jq -r '.data.pod.runtime.ports[]? | select(.privatePort==22 and .isIpPublic==true) | .ip'         2>/dev/null | head -1)
        if [[ -n "$SSH_PORT" && -n "$SSH_IP" ]]; then
            break
        fi
    fi

    sleep 5
    WAITED=$((WAITED + 5))
    printf "."
done
echo ""

if [[ -z "$SSH_PORT" ]]; then
    echo -e "${YELLOW}Pod started but SSH port not yet assigned. Check status with:${RESET}"
    echo "  ./runpod/start_ollama_pod.sh --status $POD_ID"
    exit 0
fi

# ── Print connection info ─────────────────────────────────────────────────────
SSH_CMD="ssh root@${SSH_IP} -p ${SSH_PORT} -i ${SSH_KEY} -o StrictHostKeyChecking=no"

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}  ✓ Pod ready${RESET}"
echo -e "  Pod ID  : $POD_ID"
echo -e "  SSH     : $SSH_CMD"
echo ""
echo -e "  Stop when done:   ./runpod/start_ollama_pod.sh --stop $POD_ID"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

# ── Auto-run: SSH in, set up, and launch sprint ───────────────────────────────
if $AUTO_RUN; then
    echo ""
    echo "→ Waiting for SSH to be ready..."
    SSH_READY=false
    for i in $(seq 1 24); do
        if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
               -o BatchMode=yes -i "${SSH_KEY}" \
               -p "${SSH_PORT}" "root@${SSH_IP}" "echo ok" &>/dev/null; then
            SSH_READY=true
            break
        fi
        printf "."
        sleep 5
    done
    echo ""

    if ! $SSH_READY; then
        echo -e "${YELLOW}SSH not reachable after 2 min. Connect manually:${RESET}"
        echo "  $SSH_CMD"
        exit 1
    fi

    echo "→ Connected — running setup + full sprint..."

    $SSH_CMD << REMOTE
set -e

echo "=== Installing dependencies ==="
apt-get update -qq && apt-get install -y -qq zstd pciutils

echo "=== Installing Ollama ==="
curl -fsSL https://ollama.ai/install.sh | sh

echo "=== Starting Ollama (quick-pass config: ${QUICK_WORKERS} parallel slots) ==="
OLLAMA_HOST=0.0.0.0:11434 OLLAMA_NUM_PARALLEL=${QUICK_WORKERS} OLLAMA_MAX_LOADED_MODELS=2 \
    ollama serve > /tmp/ollama.log 2>&1 &
sleep 8

echo "=== GPU check ==="
grep "inference compute" /tmp/ollama.log || echo "WARNING: no GPU line in log"

echo "=== Pulling tier-1 model: ${TIER1_MODEL} ==="
ollama pull ${TIER1_MODEL}

echo "=== Pulling tier-2 model: ${TIER2_MODEL} ==="
ollama pull ${TIER2_MODEL}

echo "=== Cloning repos ==="
[ -d ~/astro_flux ] || git clone ${ASTRO_REPO} ~/astro_flux
[ -d ~/Xanadu ]     || git clone ${XANADU_REPO} ~/Xanadu

echo "=== Installing Python deps ==="
pip install -r ~/Xanadu/sovereign_agent/requirements.txt -q

echo "=== Starting full sprint (quick: ${QUICK_WORKERS} workers → deep: ${DEEP_WORKERS} workers) ==="
~/Xanadu/sovereign_agent/supervisor.sh ~/astro_flux --full --workers ${QUICK_WORKERS}
REMOTE

else
    # Print manual setup commands
    echo ""
    echo "  Next steps — run these on the pod:"
    echo ""
    echo "  $SSH_CMD"
    echo ""
    echo "  # On the pod:"
    echo "  curl -fsSL https://ollama.ai/install.sh | sh"
    echo "  OLLAMA_HOST=0.0.0.0:11434 OLLAMA_NUM_PARALLEL=${CHOSEN_WORKERS} ollama serve &"
    echo "  ollama pull ${TIER1_MODEL}"
    echo "  ollama pull ${TIER2_MODEL}"
    echo "  git clone ${ASTRO_REPO} ~/astro_flux"
    echo "  git clone ${XANADU_REPO} ~/Xanadu"
    echo "  cd ~/Xanadu/sovereign_agent && pip install -r requirements.txt -q"
    echo "  ~/Xanadu/sovereign_agent/supervisor.sh ~/astro_flux --full --workers ${CHOSEN_WORKERS}"
fi
