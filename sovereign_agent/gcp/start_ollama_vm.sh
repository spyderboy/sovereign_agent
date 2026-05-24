#!/usr/bin/env bash
# ── Provision the persistent Ollama GPU VM on GCP ─────────────────────────────
#
# Run this ONCE from your local machine.  It:
#   1. Creates an L4 GPU VM in GCP
#   2. SSHes in, installs Ollama + CUDA drivers
#   3. Pulls the 7B and 32B models
#   4. Installs a systemd service so Ollama survives reboots
#   5. Opens the Ollama port on the internal VPC (NOT the public internet)
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
ZONE="${GCP_ZONE:-us-central1-a}"
VM_NAME="ollama-server"
MACHINE_TYPE="g2-standard-8"          # 1× L4 GPU, 32 GB RAM — fits 7B + 32B
DISK_SIZE="100GB"
IMAGE_FAMILY="debian-12"
IMAGE_PROJECT="debian-cloud"
TIER1_MODEL="${TIER1_MODEL:-qwen2.5-coder:7b-instruct-q4_K_M}"
TIER2_MODEL="${TIER2_MODEL:-qwen2.5-coder:32b}"

echo "═══════════════════════════════════════════════════"
echo "  Sovereign Agent — Ollama GPU VM provisioner"
echo "  Project : $PROJECT_ID"
echo "  Zone    : $ZONE"
echo "  VM      : $VM_NAME ($MACHINE_TYPE)"
echo "═══════════════════════════════════════════════════"

# ── 1. Create the VM ──────────────────────────────────────────────────────────
echo ""
echo "→ Creating VM $VM_NAME..."
gcloud compute instances create "$VM_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --accelerator="type=nvidia-l4,count=1" \
    --maintenance-policy=TERMINATE \
    --provisioning-model=STANDARD \
    --image-family="$IMAGE_FAMILY" \
    --image-project="$IMAGE_PROJECT" \
    --boot-disk-size="$DISK_SIZE" \
    --boot-disk-type=pd-ssd \
    --tags=ollama-server \
    --metadata=startup-script='#!/bin/bash
# Basic startup: ensure CUDA drivers persist across reboots
/opt/google/compute-engine/startup-scripts/google_metadata_script_runner startup
' \
    --scopes=cloud-platform

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
sudo tee /etc/systemd/system/ollama.service > /dev/null << 'EOF'
[Unit]
Description=Ollama LLM server
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
Environment=OLLAMA_HOST=0.0.0.0:11434
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
echo "  ✓ Ollama VM ready"
echo "  Internal IP : $INTERNAL_IP"
echo "  OLLAMA_URL  : http://$INTERNAL_IP:11434"
echo ""
echo "  Set this in your worker env:"
echo "    OLLAMA_URL=http://$INTERNAL_IP:11434"
echo ""
echo "  Stop VM when sprint is done:"
echo "    gcloud compute instances stop $VM_NAME --zone $ZONE"
echo "═══════════════════════════════════════════════════"
