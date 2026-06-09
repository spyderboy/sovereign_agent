#!/usr/bin/env bash
# ── Sovereign Agent — on-pod setup + sprint + push ────────────────────────────
#
# One-liner (run on the pod):
#   WORKERS=20 bash <(curl -s https://raw.githubusercontent.com/spyderboy/sovereign_agent/main/sovereign_agent/runpod/setup_pod.sh)
#
# Env vars (all optional):
#   MODEL         Ollama model tag  (default: qwen2.5-coder:14b-instruct-q4_K_M)
#   WORKERS       Parallel workers  (default: 10)
#   DEEP_WORKERS  Deep-pass workers (default: 3)
#   SKIP_SPRINT   Set to 1 to install only, skip sprint
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODEL="${MODEL:-qwen2.5-coder:14b-instruct-q4_K_M}"
WORKERS="${WORKERS:-10}"
DEEP_WORKERS="${DEEP_WORKERS:-3}"
ASTRO_REPO="https://github.com/spyderboy/astro_flux.git"
AGENT_REPO="https://github.com/spyderboy/sovereign_agent.git"
SKIP_SPRINT="${SKIP_SPRINT:-0}"
FLUTTER_VERSION="3.29.2"
FLUTTER_URL="https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz"

BOLD="\033[1m"; GREEN="\033[92m"; RED="\033[91m"; RESET="\033[0m"
step() { echo -e "\n${BOLD}=== $1 ===${RESET}"; }

step "[1/8] System deps"
apt-get update -qq
apt-get install -y -qq zstd pciutils curl unzip xz-utils zip libglu1-mesa git

step "[2/8] Python symlink"
ln -sf /usr/bin/python3 /usr/bin/python

step "[3/8] Ollama"
curl -fsSL https://ollama.ai/install.sh | sh

step "[4/8] Starting Ollama (GPU check)"
OLLAMA_HOST=0.0.0.0:11434 OLLAMA_NUM_PARALLEL=${WORKERS} OLLAMA_MAX_LOADED_MODELS=1 \
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
        echo -e "\n${RED}ERROR: GPU not detected${RESET}"; cat /tmp/ollama.log; exit 1
    fi
done

step "[5/8] Flutter ${FLUTTER_VERSION}"
if ! command -v flutter &>/dev/null; then
    cd /opt
    curl -sO "${FLUTTER_URL}"
    tar xf "flutter_linux_${FLUTTER_VERSION}-stable.tar.xz"
    rm -f "flutter_linux_${FLUTTER_VERSION}-stable.tar.xz"
    git config --global --add safe.directory /opt/flutter
    ln -sf /opt/flutter/bin/flutter /usr/local/bin/flutter
    ln -sf /opt/flutter/bin/dart /usr/local/bin/dart
    flutter precache --no-ios --no-android --no-web
fi
echo -e "${GREEN}✓ $(flutter --version | head -1)${RESET}"

step "[6/8] Pull model"
ollama pull "${MODEL}"
echo -e "${GREEN}✓ Model ready${RESET}"

step "[7/8] Repos & deps"
# Prune orphaned Ollama blobs (leftover from deleted models eat GBs)
if [ -d ~/.ollama/models/blobs ]; then
    find ~/.ollama/models/manifests -type f -exec cat {} \; 2>/dev/null \
        | grep -oP 'sha256:[a-f0-9]+' | sed 's/sha256:/sha256-/' | sort -u > /tmp/needed_blobs.txt
    cd ~/.ollama/models/blobs
    for f in sha256-*; do
        grep -qF "$f" /tmp/needed_blobs.txt 2>/dev/null || rm -f "$f"
    done
    cd -
fi

[ -d ~/astro_flux ]     || git clone "${ASTRO_REPO}" ~/astro_flux
[ -d ~/sovereign_agent ] || git clone "${AGENT_REPO}" ~/sovereign_agent
cd ~/astro_flux      && git pull --ff-only 2>/dev/null || true
cd ~/sovereign_agent && git pull --ff-only 2>/dev/null || true
pip install -r ~/sovereign_agent/sovereign_agent/requirements.txt -q --break-system-packages 2>/dev/null || \
    pip install -r ~/sovereign_agent/sovereign_agent/requirements.txt -q
cd ~/astro_flux && flutter pub get

UNCHECKED=$(grep -c "^\- \[ \]" ~/astro_flux/ROADMAP.md 2>/dev/null || echo "?")
echo -e "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}  ✓ Setup complete — ${UNCHECKED} tasks queued — ${WORKERS} workers${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

[[ "${SKIP_SPRINT}" == "1" ]] && exit 0

step "[8/8] Sprint"
# Mount logs dir in RAM so trace writes never fill the container disk
mkdir -p ~/astro_flux/logs
mount -t tmpfs -o size=500M tmpfs ~/astro_flux/logs/ 2>/dev/null || true
echo "Logs dir: $(df -h ~/astro_flux/logs/ | tail -1)"

cd ~/astro_flux
TIER1_MODEL="${MODEL}" TIER2_MODEL="${MODEL}" DEEP_WORKERS="${DEEP_WORKERS}" \
    ~/sovereign_agent/sovereign_agent/supervisor.sh ~/astro_flux --full --workers "${WORKERS}"

# ── Auto-commit and push when sprint finishes ─────────────────────────────────
step "Committing and pushing results"
cd ~/astro_flux
git config user.email "spyderboy@gmail.com"
git config user.name "Tony"

# Stage everything except generated/log files
git add -A
git restore --staged .dart_tool/ logs/ .flutter-plugins .flutter-plugins-dependencies pubspec.lock 2>/dev/null || true

DONE=$(grep -c "^\- \[x\]" ROADMAP.md 2>/dev/null || echo "?")
git commit -m "Sprint: ${DONE} tasks completed" || echo "Nothing new to commit"

# Push to whichever branch exists (master or main)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git push origin "${BRANCH}"
echo -e "${GREEN}✓ Pushed to ${BRANCH}${RESET}"
