# Dapr Sidecar Initialization for macOS

This guide explains how to initialize Diagrid/Dapr locally on macOS as a sidecar process.

## Overview

Dapr (Distributed Application Runtime) can be run as a sidecar in your local development environment. This setup allows you to use Dapr's distributed capabilities (state store, pub/sub, etc.) without deploying to cloud infrastructure.

## Prerequisites

- Python 3.x installed
- macOS with Homebrew (optional)

## Installation Steps

### 1. Install Dapr CLI

Download and install Dapr CLI to a local directory:

```bash
curl -sL https://github.com/dapr/cli/releases/download/v1.17.1/dapr_darwin_arm64.tar.gz -o /tmp/dapr.tar.gz
tar -xzf /tmp/dapr.tar.gz -C /tmp
mkdir -p ./bin
cp /tmp/dapr ./bin/dapr
chmod +x ./bin/dapr
```

### 2. Initialize Dapr Sidecar

Run the initialization script:

```bash
chmod +x dapr-init.sh
./dapr-init.sh
```

Or manually start Dapr:

```bash
./bin/dapr run --app-id sovereign-agent --app-port 5000 --resources-path ./components -- python3 app.py
```

### 3. Verify Dapr is Running

```bash
curl http://localhost:5000/v1.0/metadata
```

## Configuration

Update your `.env` file with the appropriate port:

```env
DAPR_PORT=5000
DIAGRID_URL=http://localhost:5000
```

## Usage Examples

### Start Dapr Sidecar

```bash
./dapr-init.sh
```

### Stop Dapr Sidecar

```bash
pkill -f "dapr run"
# or
kill -9 <PID>
```

### Check Dapr Status

```bash
curl http://localhost:5000/v1.0/metadata
```

## Diagrid Integration

The Dapr sidecar is integrated with the LangGraph workflow:

```python
from app import runner

# Invoke the graph with Dapr-backed persistence
result = runner.invoke({"backlog_path": "./backlog.md"})
print(f"Result: {result}")
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1.0/metadata` | GET | Get Dapr metadata |
| `/state` | GET | Get current state |
| `/state/set` | POST | Set state values |
| `/state/update` | POST | Update state values |

## Running the Orchestrator

```bash
./bin/dapr run --app-id sovereign-agent --app-port 5000 --resources-path ./components -- python3 app.py
```

## Components

The `./components/statestore.yaml` file configures an in-memory state store:

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.in-memory
  metadata:
  - name: evictionPolicy
    value: none
```

## Troubleshooting

- **Port already in use**: Change `DAPR_PORT` in `.env` to a different port
- **Connection refused**: Ensure Dapr has finished initializing (wait ~5 seconds after start)
- **Permission errors**: Run the script with appropriate permissions

## Notes

- Dapr sidecar runs as a background process
- State persists in memory until the process is stopped
- For production use, consider deploying to cloud infrastructure instead of local development mode

## Verification

The Dapr sidecar is now running and ready to serve as the Diagrid "nervous system" for state management and pub/sub functionality.

```bash
# Check health
curl http://localhost:5000/v1.0/metadata

# Run the orchestrator
./bin/dapr run --app-id sovereign-agent --app-port 5000 --resources-path ./components -- python3 app.py
```
