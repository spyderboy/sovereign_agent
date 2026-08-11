#!/bin/bash

# Dapr Sidecar Initialization Script for macOS
# This script sets up Dapr as a sidecar process for local development

set -e

echo "=== Dapr Sidecar Initialization ==="

# Configuration from .env
DAPR_PORT=${DAPR_PORT:-5000}
DIAGRID_URL="http://localhost:${DAPR_PORT}"

echo "Starting Dapr sidecar on port ${DAPR_PORT}..."

# Check if dapr is already running
if pgrep -f "dapr run" > /dev/null; then
    echo "Dapr is already running. Stopping existing instance..."
    pkill -f "dapr run" || true
    sleep 2
fi

# Start Dapr sidecar with in-memory state store
echo "Launching Dapr sidecar process..."
source .venv/bin/activate
./bin/dapr run \
    --app-id sovereign-agent \
    --app-port ${DAPR_PORT} \
    --resources-path ./components \
    -- python3 app.py &

DAPR_PID=$!
echo "Dapr sidecar started with PID: ${DAPR_PID}"

# Wait for Dapr to be ready
echo "Waiting for Dapr to initialize..."
for i in {1..30}; do
    if curl -s "http://localhost:${DAPR_PORT}/v1.0/metadata" > /dev/null 2>&1; then
        echo "Dapr is ready!"
        break
    fi
    sleep 1
done

if [ ${i} -eq 30 ]; then
    echo "Warning: Dapr may not be fully ready yet. Please wait a moment."
fi

echo ""
echo "=== Dapr Sidecar Status ==="
echo "App ID: sovereign-agent"
echo "App Port: ${DAPR_PORT}"
echo "PID: ${DAPR_PID}"
echo ""
echo "To stop Dapr, run: kill -9 ${DAPR_PID} || pkill -f 'dapr run'"
