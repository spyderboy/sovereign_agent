# ── Sovereign Agent — GCP Worker Image ────────────────────────────────────────
# One image handles both tiers — differentiated by WORKER_TIER at runtime.
#
# Tier 1 (7B):  Ollama starts embedded in the container. Deploy on Cloud Run
#               Jobs with a T4 GPU attached (pay-per-second, zero idle cost).
#
# Tier 2 (32B+Claude): 32B doesn't fit on a T4. Deploy on a GCE VM with an
#               L4 GPU, with Ollama running externally. Set OLLAMA_EMBEDDED=false
#               and pass OLLAMA_URL pointing to the VM's internal IP.
#               Claude escalation runs inline (no separate queue needed).
#
# Build:
#   docker build -t sovereign-worker .
#   docker tag sovereign-worker gcr.io/<project>/sovereign-worker:latest
#   docker push gcr.io/<project>/sovereign-worker:latest
#
# Run tier-1 worker (Cloud Run Job, embedded Ollama):
#   docker run --gpus all --rm \
#     -e WORKER_TIER=1 \
#     -e OLLAMA_EMBEDDED=true \
#     -e PROJECT_REPO=https://github.com/you/astro_flux.git \
#     -e FIRESTORE_PROJECT_ID=astro-flux-spyderboy \
#     sovereign-worker
#
# Run tier-2 worker (GCE VM, external Ollama):
#   docker run --rm \
#     -e WORKER_TIER=2 \
#     -e OLLAMA_EMBEDDED=false \
#     -e OLLAMA_URL=http://<l4-vm-internal-ip>:11434 \
#     -e ANTHROPIC_API_KEY=sk-ant-... \
#     -e PROJECT_REPO=https://github.com/you/astro_flux.git \
#     -e FIRESTORE_PROJECT_ID=astro-flux-spyderboy \
#     sovereign-worker

FROM python:3.12-slim

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        unzip \
        xz-utils \
        zstd \
        libglu1-mesa \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Ollama ────────────────────────────────────────────────────────────────────
# Installed so tier-1 workers can start Ollama embedded (OLLAMA_EMBEDDED=true).
# Tier-2 workers use an external Ollama on a GCE VM and don't invoke this binary.
# The actual model weights are NOT baked into the image — they're pulled at
# first startup and cached in the Cloud Run Job's ephemeral storage.
RUN curl -fsSL https://ollama.ai/install.sh | sh

# ── Flutter SDK ───────────────────────────────────────────────────────────────
# Required for `flutter analyze` — the validation step after every task attempt.
# Cloned from stable channel so no hardcoded version number is needed.
ENV FLUTTER_HOME=/opt/flutter
ENV PATH="${FLUTTER_HOME}/bin:${PATH}"

RUN git clone --depth 1 --branch stable https://github.com/flutter/flutter.git /opt/flutter \
    && flutter config --no-analytics \
    && flutter --version

# ── Python deps ───────────────────────────────────────────────────────────────
WORKDIR /app/sovereign_agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Sovereign agent source ────────────────────────────────────────────────────
COPY . .

# ── Runtime env (all overridable via -e at docker run time) ──────────────────

# Which tier this container handles: 1 (7B) or 2 (32B + inline Claude)
ENV WORKER_TIER=1

# Project repo to clone fresh on each run
ENV PROJECT_REPO=""
ENV PROJECT_BRANCH="main"
ENV PROJECT_DIR="/app/project"

# Ollama mode:
#   true  = start Ollama embedded in this container (Cloud Run Jobs + T4)
#   false = connect to external Ollama at OLLAMA_URL (persistent L4 VM)
ENV OLLAMA_EMBEDDED=true
ENV OLLAMA_URL="http://localhost:11434"

# Model names
ENV TIER1_MODEL="qwen2.5-coder:7b-instruct-q4_K_M"
ENV TIER2_MODEL="qwen2.5-coder:32b"
ENV CLAUDE_MODEL="claude-sonnet-4-6"

# Anthropic key — only used by tier-2 workers for inline Claude escalation
ENV ANTHROPIC_API_KEY=""

# GCP identifiers
ENV FIRESTORE_PROJECT_ID=""
ENV GCP_PROJECT=""

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY gcp/worker_entrypoint.sh /usr/local/bin/worker_entrypoint.sh
RUN chmod +x /usr/local/bin/worker_entrypoint.sh

ENTRYPOINT ["/usr/local/bin/worker_entrypoint.sh"]
