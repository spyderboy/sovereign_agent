#!/usr/bin/env bash
# ── Provision the Ollama GPU VM on GCP (A100 40 GB Spot) ──────────────────────
#
# Run this ONCE from your local machine.  It:
#   1. Creates an A100 40GB Spot VM in GCP (~$1.10/hr)
#   2. SSHes in, installs Ollama + CUDA drivers
#   3. Pulls the 7B coder and 35B MoE models
#   4. Installs a systemd service so Ollama survives reboots
#   5. Opens the Ollama port on the internal VPC (NOT the public internet)
#
# Spot note: VM may be preempted. The supervisor handles this gracefully —
# ROADMAP.md checkboxes are the source of truth; just re-run to resume.
#
# Prerequisites:
#   gcloud auth login && gcloud config set project <your-project>
#
# Usage:
#   chmod +x gcp/start_ollama_vm.sh
#   ./gcp/start_ollama_vm.sh
#
# Estimated cost: ~$0.54/hr (L4 GPU, us-central1)
# Stop the VM when not running a sprint to save money:
#   gcloud compute instances stop ollama-server --zone us-central1-a
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Config (edit these) ───────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT:-astro-flux-spyderboy}"
DISK_SIZE="150GB"
IMAGE_FAMILY="debian-12"
IMAGE_PROJECT="debian-cloud"
TIER1_MODEL="${TIER1_MODEL:-qwen2.5-coder:7b-instruct-q4_K_M}"
TIER2_MODEL="${TIER2_MODEL:-qwen3.6:35b-a3b}"

# GPU candidates — tried in order until one succeeds
# Format: "vm-name|machine-type|accelerator|zone|workers|cost-note"
GPU_CANDIDATES=(
    "ollama-a100|a2-highgpu-1g|nvidia-tesla-a100|us-central1-a|4|A100 40GB Spot ~\$1.10/hr"
    "ollama-a100|a2-highgpu-1g|nvidia-tesla-a100|us-east1-b|4|A100 40GB Spot ~\$1.10/hr"
    "ollama-a100|a2-highgpu-1g|nvidia-tesla-a100|europe-west4-a|4|A100 40GB Spot ~\$1.10/hr"
    "ollama-l4|g2-standard-8|nvidia-l4|us-central1-a|2|L4 24GB Spot ~\$0.16/hr"
    "ollama-l4|g2-standard-8|nvidia-l4|us-central1-b|2|L4 24GB Spot ~\$0.16/hr"
    "ollama-l4|g2-standard-8|nvidia-l4|us-east1-b|2|L4 24GB Spot ~\$0.16/hr"
)

echo "═══════════════════════════════════════════════════"
echo "  Sovereign Agent — Ollama GPU VM provisioner"
echo "  Project : $PROJECT_ID"
echo "  Trying GPU candidates in order..."
echo "═══════════════════════════════════════════════════"

# ── 1. Create the VM — try each candidate until one succeeds ─────────────────
VM_NAME="" MACHINE_TYPE="" ACCELERATOR="" ZONE="" OLLAMA_PARALLEL=""

for candidate in "${GPU_CANDIDATES[@]}"; do
    IFS='|' read -r _name _machine _accel _zone _workers _note <<< "$candidate"
    echo ""
    echo "→ Trying $_note  (zone: $_zone)..."
    if gcloud compute instances create "$_name" \
        --project="$PROJECT_ID" \
        --zone="$_zone" \
        --machine-type="$_machine" \
        --accelerator="type=${_accel},count=1" \
        --maintenance-policy=TERMINATE \
        --provisioning-model=SPOT \
        --instance-termination-action=STOP \
        --image-family="$IMAGE_FAMILY" \
        --image-project="$IMAGE_PROJECT" \
        --boot-disk-size="$DISK_SIZE" \
        --boot-disk-type=pd-ssd \
        --tags=ollama-server \
        --scopes=cloud-platform 2>/dev/null; then
        VM_NAME="$_name"
        MACHINE_TYPE="$_machine"
        ACCELERATOR="$_accel"
        ZONE="$_zone"
        OLLAMA_PARALLEL="$_workers"
        echo "✓ VM created: $_name ($_note)"
        break
    else
        echo "  ✗ quota unavailable — trying next option..."
    fi
done

if [[ -z "$VM_NAME" ]]; then
    echo ""
    echo "ERROR: All GPU options exhausted. Options:"
    echo "  1. Request A100 quota: https://console.cloud.google.com/iam-admin/quotas"
    echo "     Filter: NVIDIA_A100_GPUS → Edit Quotas → request 1 in us-central1"
    echo "  2. Check L4 quota:     filter NVIDIA_L4_GPUS"
    echo "  3. Re-run after quota is approved (usually same-day for small requests)"
    exit 1
fi

echo "✓ VM created. Waiting 30 s for SSH to come up..."
sleep 30

# ── 2. Install Ollama + CUDA + models via SSH ─────────────────────────────────
echo ""
echo "→ Installing Ollama, CUDA drivers, and pulling models (this takes ~10 min)..."

gcloud compute ssh "$VM_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --command="bash -s" << REMOTE_SCRIPT
set -euo pipefail

echo "  Installing CUDA drivers..."
curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/cuda-keyring_1.1-1_all.deb \
    -o /tmp/cuda-keyring.deb
sudo dpkg -i /tmp/cuda-keyring.deb
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends cuda-drivers nvidia-cuda-toolkit

echo "  Installing Ollama..."
curl -fsSL https://ollama.ai/install.sh | sudo sh

echo "  Writing systemd service for Ollama (listen on all interfaces)..."
sudo tee /etc/systemd/system/ollama.service > /dev/null << EOF
[Unit]
Description=Ollama LLM server
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
Environment=OLLAMA_HOST=0.0.0.0:11434
Environment=OLLAMA_NUM_PARALLEL=${OLLAMA_PARALLEL}
Environment=OLLAMA_MAX_LOADED_MODELS=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl start ollama
sleep 5   # give ollama a moment before pulling

echo "  Pulling tier-1 model: ${TIER1_MODEL}..."
ollama pull ${TIER1_MODEL}

echo "  Pulling tier-2 model: ${TIER2_MODEL} (large — ~20 GB, may take several minutes)..."
ollama pull ${TIER2_MODEL}

echo "  ✓ Models ready."
ollama list
REMOTE_SCRIPT

# ── 3. Firewall rule — internal VPC only ──────────────────────────────────────
echo ""
echo "→ Creating firewall rule (internal VPC → port 11434)..."
gcloud compute firewall-rules create allow-ollama-internal \
    --project="$PROJECT_ID" \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:11434 \
    --source-ranges=10.0.0.0/8 \
    --target-tags=ollama-server \
    2>/dev/null || echo "  (firewall rule already exists — skipping)"

# ── 4. Print the internal IP ──────────────────────────────────────────────────
INTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --format="get(networkInterfaces[0].networkIP)")

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✓ Ollama VM ready  (A100 40GB Spot ~\$1.10/hr)"
echo "  VM name     : $VM_NAME"
echo "  Internal IP : $INTERNAL_IP"
echo ""
echo "  SSH in and run the sprint:"
echo "    gcloud compute ssh $VM_NAME --zone $ZONE"
echo "    cd ~/astro_flux"
echo "    OLLAMA_HOST=http://localhost:11434 \\"
echo "    ../Xanadu/sovereign_agent/supervisor.sh . --workers 4 --deep"
echo ""
echo "  Stop VM when sprint is done (saves cost):"
echo "    gcloud compute instances stop $VM_NAME --zone $ZONE"
echo ""
echo "  Restart a stopped VM (Spot survives stop; models stay on disk):"
echo "    gcloud compute instances start $VM_NAME --zone $ZONE"
echo "═══════════════════════════════════════════════════"
