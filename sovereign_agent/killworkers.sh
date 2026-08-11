#!/usr/bin/env bash
# Fully stop a sovereign_agent run. Ctrl-C in the terminal only kills the
# FOREGROUND process — the supervisor + workers survive and keep running
# `git checkout -f` / `git clean` on the project repo, which wipes any
# uncommitted scaffolding. ALWAYS run this after Ctrl-C, and before editing or
# committing the project repo. It exits 0 only when nothing is left alive.
pkill -9 -f work.py 2>/dev/null
pkill -9 -f supervisor.sh 2>/dev/null
pkill -9 -f orchestrate.py 2>/dev/null
sleep 1
n=$(ps aux | grep -E '[w]ork\.py|[s]upervisor\.sh|[o]rchestrate\.py' | wc -l | tr -d ' ')
if [ "$n" = "0" ]; then
  echo "✓ all harness processes stopped — safe to scaffold/commit"
  exit 0
else
  echo "✗ $n harness process(es) STILL ALIVE — do not touch the repo:"
  ps aux | grep -E '[w]ork\.py|[s]upervisor\.sh|[o]rchestrate\.py'
  exit 1
fi
