#!/usr/bin/env bash
# poll_status.sh — Read supervisor status + recent log tail for Claude's polling loop.
# Prints a compact snapshot so Claude can decide what to do next.
# Usage: ./poll_status.sh ~/Code/astro_flux

PROJECT="${1:-$(pwd)}"
LOGDIR="$PROJECT/logs"
STATUS="$LOGDIR/supervisor.status"
ESCALATE="$LOGDIR/escalate.md"

echo "=== STATUS ==="
cat "$STATUS" 2>/dev/null || echo "no_status_file"

echo ""
echo "=== ESCALATE.MD (if present) ==="
if [ -f "$ESCALATE" ]; then
    cat "$ESCALATE"
else
    echo "(none)"
fi

echo ""
echo "=== SUPERVISOR LOG (last 30 lines) ==="
tail -30 "$LOGDIR/supervisor.log" 2>/dev/null || echo "(no log yet)"
