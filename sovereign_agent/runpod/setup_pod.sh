#!/usr/bin/env bash
# ── Sovereign Agent — on-pod setup script ─────────────────────────────────────
#
# Run this on a fresh RunPod GPU pod to install everything and launch a sprint.
#
# One-liner (run on the pod):
#   bash <(curl -s https://raw.githubusercontent.com/spyderboy/Xanadu/master/sovereign_agent/runpod/setup_pod.sh)
#
# Or with custom options:
#   WORKERS=6 MODEL=qwen2.5-coder:14b-instruct-q4_K_M bash <(curl -s ...)
#
# Env vars (all optional — sensible defaults):
#   MODEL         Ollama model tag  (default: qwen2.5-coder:14b-instruct-q4_K_M)
#   WORKERS       Parallel workers  (default: 4)
#   DEEP_WORKERS  Deep-pass workers (default: 2)
#   ASTRO_REPO    astro_flux git URL
#   XANADU_REPO   Xanadu git URL
#   SKIP_SPRINT   Set to 1 to install only, don't start sprint
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODEL="${MODEL:-qwen2.5-coder:14b-instruct-q4_K_M}"
WORKERS="${WORKERS:-4}"
DEEP_WORKERS="${DEEP_WORKERS:-2}"
ASTRO_REPO="${ASTRO_REPO:-https://github.com/spyderboy/astro_flux.git}"
XANADU_REPO="${XANADU_REPO:-https://github.com/spyderboy/Xanadu.git}"
SKIP_SPRINT="${SKIP_SPRINT:-0}"
FLUTTER_VERSION="3.29.2"
FLUTTER_TARBALL="flutter_linux_${FLUTTER_VERSION}-stable.tar.xz"
FLUTTER_URL="https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/${FLUTTER_TARBALL}"

BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; RESET="\033[0m"
step() { echo -e "\n${BOLD}=== $1 ===${RESET}"; }

step "[1/8] System deps"
apt-get update -qq
apt-get install -y -qq zstd pciutils curl unzip xz-utils zip libglu1-mesa git

step "[2/8] Python symlink"
ln -sf /usr/bin/python3 /usr/bin/python

step "[3/8] Ollama"
curl -fsSL https://ollama.ai/install.sh | sh

step "[4/8] Starting Ollama (GPU check)"
OLLAMA_HOST=0.0.0.0:11434 OLLAMA_NUM_PARALLEL=${WORKERS} OLLAMA_MAX_LOADED_MODELS=2 \
    ollama serve > /tmp/ollama.log 2>&1 &
echo -n "Waiting for Ollama..."
for i in $(seq 1 12); do
    sleep 3
    if grep -q "inference compute" /tmp/ollama.log 2>/dev/null; then
        echo -e " ${GREEN}✓ GPU detected${RESET}"
        grep "inference compute" /tmp/ollama.log
        break
    fi
    printf "."
    if [[ $i -eq 12 ]]; then
        echo -e "\n${RED}ERROR: GPU not detected after 36s${RESET}"
        cat /tmp/ollama.log
        exit 1
    fi
done

step "[5/8] Flutter ${FLUTTER_VERSION}"
if ! command -v flutter &>/dev/null; then
    cd /opt
    echo "Downloading Flutter..."
    curl -sO "${FLUTTER_URL}"
    tar xf "${FLUTTER_TARBALL}"
    rm -f "${FLUTTER_TARBALL}"
    git config --global --add safe.directory /opt/flutter
    ln -sf /opt/flutter/bin/flutter /usr/local/bin/flutter
    ln -sf /opt/flutter/bin/dart /usr/local/bin/dart
    flutter precache --no-ios --no-android --no-web
else
    echo "Flutter already installed: $(flutter --version | head -1)"
fi
echo -e "${GREEN}✓ Flutter: $(flutter --version | head -1)${RESET}"

step "[6/8] Pull model: ${MODEL}"
ollama pull "${MODEL}"
echo -e "${GREEN}✓ Model ready${RESET}"

step "[7/8] Repos & deps"
[ -d ~/astro_flux ] || git clone "${ASTRO_REPO}" ~/astro_flux
[ -d ~/Xanadu ]     || git clone "${XANADU_REPO}" ~/Xanadu

# Pull latest if repos already exist
cd ~/astro_flux && git pull --ff-only 2>/dev/null || true
cd ~/Xanadu     && git pull --ff-only 2>/dev/null || true

pip install -r ~/Xanadu/sovereign_agent/requirements.txt -q --break-system-packages 2>/dev/null || \
    pip install -r ~/Xanadu/sovereign_agent/requirements.txt -q

cd ~/astro_flux && flutter pub get
echo -e "${GREEN}✓ Repos and deps ready${RESET}"

# ── Summary ────────────────────────────────────────────────────────────────────
UNCHECKED=$(grep -c "^\- \[ \]" ~/astro_flux/ROADMAP.md 2>/dev/null || echo "?")
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}  ✓ Setup complete${RESET}"
echo -e "  Model   : ${MODEL}"
echo -e "  Workers : ${WORKERS} quick, ${DEEP_WORKERS} deep"
echo -e "  Tasks   : ${UNCHECKED} unchecked in ROADMAP.md"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

if [[ "${SKIP_SPRINT}" == "1" ]]; then
    echo ""
    echo "SKIP_SPRINT=1 — setup only. To start:"
    echo "  TIER1_MODEL=${MODEL} TIER2_MODEL=${MODEL} DEEP_WORKERS=${DEEP_WORKERS} \\"
    echo "    ~/Xanadu/sovereign_agent/supervisor.sh ~/astro_flux --full --workers ${WORKERS}"
    exit 0
fi

step "[8/8] Sprint — ${WORKERS} workers × ${MODEL}"
TIER1_MODEL="${MODEL}" \
TIER2_MODEL="${MODEL}" \
DEEP_WORKERS="${DEEP_WORKERS}" \
    ~/Xanadu/sovereign_agent/supervisor.sh ~/astro_flux --full --workers "${WORKERS}"
