#!/bin/bash
# Start the Xanadu console and open it in the browser.
#   ./console.sh            (port 8765)
#   ./console.sh 9000
cd "$(dirname "$0")"
PORT="${1:-8765}"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
( sleep 1.5; open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null ) &
exec "$PY" console.py --port "$PORT"
