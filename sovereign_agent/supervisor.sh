#!/usr/bin/env bash
# supervisor.sh — Run work.py in a loop; supports parallel workers.
#
# Usage (sequential, original behaviour):
#   ./supervisor.sh ~/Code/astro_flux
#   ./supervisor.sh ~/Code/astro_flux --features-only
#
# Usage (parallel — N workers share the task list by stride):
#   ./supervisor.sh ~/Code/astro_flux --workers 4
#   ./supervisor.sh ~/Code/astro_flux --workers 4 --features-only
#
# Usage (quick/deep split — tier-1 fast sweep then tier-2+ mop-up):
#   ./supervisor.sh ~/Code/astro_flux --quick              # tier-1 only; failures → tier2_queue.jsonl
#   ./supervisor.sh ~/Code/astro_flux --deep               # tier-2+ on queued failures only
#   ./supervisor.sh ~/Code/astro_flux --workers 4 --quick  # parallel quick sweep
#   ./supervisor.sh ~/Code/astro_flux --workers 2 --deep   # parallel deep mop-up
#
# Each worker K handles tasks at positions K, K+N, K+2N … (round-robin).
# OLLAMA_NUM_PARALLEL should be set to at least N for the 4B model calls.
#
# Escalation (parallel mode):
#   Any worker that hits a structural error writes logs/escalate-wK.md and exits 2.
#   Remaining workers finish their tasks.  Once all workers are done the supervisor
#   prints a summary and waits for you to write "fixed" to logs/supervisor.status.

set -uo pipefail

PROJECT="${1:-$(pwd)}"
[ $# -gt 0 ] && shift
SOVEREIGN="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$PROJECT/logs"
STATUS="$LOGDIR/supervisor.status"
LOG="$LOGDIR/supervisor.log"

BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; DIM="\033[2m"; RESET="\033[0m"

mkdir -p "$LOGDIR"

# ── resolve Python: prefer venv, fall back to system python3 ─────────────────
VENV_PYTHON="$SOVEREIGN/.venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="python3"
fi

# ── ensure Python deps ────────────────────────────────────────────────────────
REQS="$SOVEREIGN/requirements.txt"
if [ -f "$REQS" ]; then
    "$PYTHON" -c "import dotenv, requests, anthropic" 2>/dev/null || {
        echo -e "${YELLOW}Installing missing Python dependencies...${RESET}"
        "$PYTHON" -m pip install -q -r "$REQS" 2>&1 | tail -5
    }
fi

# ── parse --workers N from args, pass the rest through to work.py ─────────────
WORKERS=1
PASS_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers)
            WORKERS="${2:?--workers requires a number}"
            if ! [[ "$WORKERS" =~ ^[0-9]+$ ]]; then
                echo "Error: --workers requires a positive integer, got: '$WORKERS'" >&2
                exit 1
            fi
            shift 2
            ;;
        *)
            PASS_ARGS+=("$1")
            shift
            ;;
    esac
done

# ── deep pass: limit workers on Apple Silicon (VRAM thrash); allow on Linux ──
IS_DEEP=false
for arg in "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"; do
    [[ "$arg" == "--deep" ]] && IS_DEEP=true
done
# On macOS (Apple Silicon unified memory) multiple deep workers thrash VRAM.
# On Linux (GCP A100/L4) OLLAMA_NUM_PARALLEL handles concurrency — allow it.
if $IS_DEEP && [[ "$WORKERS" -gt 1 ]] && [[ "$(uname)" == "Darwin" ]]; then
    echo -e "${YELLOW}⚠  --deep with multiple workers causes VRAM thrashing on Apple Silicon."
    echo -e "   Forcing --workers 1 for the deep pass.${RESET}"
    WORKERS=1
fi

log() {
    local msg="[$(date '+%H:%M:%S')] $*"
    echo -e "$msg" | tee -a "$LOG"
}

header() {
    local mode=""
    [ "$WORKERS" -gt 1 ] && mode="  ${WORKERS} parallel workers"
    echo -e "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  🤖  Supervisor — $(date '+%Y-%m-%d')${RESET}"
    echo -e "${BOLD}  Project: $PROJECT${mode}${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
}

write_status() { echo "$1" > "$STATUS"; }
read_status()  { cat "$STATUS" 2>/dev/null || echo "unknown"; }

has_unchecked_tasks() {
    grep -q '^- \[ \]' "$PROJECT/ROADMAP.md" 2>/dev/null
}

# In --deep mode the workers only drain tier2_queue.jsonl, not ROADMAP.md.
# Use this check instead of has_unchecked_tasks when running --deep.
has_queued_tasks() {
    local queue="$PROJECT/logs/tier2_queue.jsonl"
    [ -f "$queue" ] && [ -s "$queue" ]
}

is_deep_mode() {
    for arg in "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"; do
        [ "$arg" = "--deep" ] && return 0
    done
    return 1
}

more_work_remains() {
    if is_deep_mode; then
        has_queued_tasks
    else
        has_unchecked_tasks
    fi
}

run_promote_rules() {
    log "Running promote_rules.py to apply learned rules..."
    "$PYTHON" "$SOVEREIGN/promote_rules.py" --project "$PROJECT" --threshold 2 \
        2>&1 | tee -a "$LOG" | grep -E "promoted|Nothing|candidate|rule" || true
}

# ── shared state ──────────────────────────────────────────────────────────────
MAX_ESCALATIONS=8
ESCALATION_COUNT=0
LAST_ESCALATION_TASK=0

# All live worker PIDs — used by the Ctrl+C trap
declare -a WORK_PIDS=()

trap '
    echo -e "\n  Supervisor interrupted."
    for _pid in "${WORK_PIDS[@]+"${WORK_PIDS[@]}"}"; do
        kill "$_pid" 2>/dev/null || true
    done
    write_status "stopped"
    exit 130
' INT TERM

header
log "Supervisor started. Project=$PROJECT  Sovereign=$SOVEREIGN  Workers=$WORKERS"
write_status "running"

# ══════════════════════════════════════════════════════════════════════════════
# ── PARALLEL path (--workers N, N > 1) ────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$WORKERS" -gt 1 ]]; then

    while true; do
        WORK_PIDS=()
        declare -a WORKER_EXITS=()

        log "▶  Spawning $WORKERS parallel workers  (stride=$WORKERS)"
        # Budget multiplier scales with workers to absorb Ollama queue depth,
        # but cap at 6 — beyond that you're just letting stuck tasks run forever.
        BUDGET_MULT=$(( WORKERS < 6 ? WORKERS : 6 ))
        for ((w=0; w<WORKERS; w++)); do
            PYTHONUNBUFFERED=1 "$PYTHON" "$SOVEREIGN/work.py" \
                --project "$PROJECT" \
                --worker-id "$w" \
                --stride "$WORKERS" \
                --budget-multiplier "$BUDGET_MULT" \
                "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}" \
                > >(sed -u "s/^/[w${w}] /" | tee -a "$LOG") 2>&1 &
            WORK_PIDS+=($!)
        done

        # Wait for every worker and collect exit codes
        any_interrupted=0
        any_escalation=0
        for ((w=0; w<WORKERS; w++)); do
            wait "${WORK_PIDS[$w]}" || true
            WORKER_EXITS[$w]=$?
            log "Worker $w finished (exit ${WORKER_EXITS[$w]})"

            if [ "${WORKER_EXITS[$w]}" -eq 130 ]; then
                any_interrupted=1
            elif [ "${WORKER_EXITS[$w]}" -eq 2 ]; then
                any_escalation=1
                ESCALATION_COUNT=$((ESCALATION_COUNT + 1))
                log "${YELLOW}Worker $w escalated — see $LOGDIR/escalate-w${w}.md${RESET}"
            fi
        done
        WORK_PIDS=()

        # Honour Ctrl+C from any worker
        if [ $any_interrupted -eq 1 ]; then
            write_status "stopped"
            exit 130
        fi

        # Close the learning loop after every batch
        run_promote_rules

        # ── escalation handling ───────────────────────────────────────────────
        if [ $any_escalation -eq 1 ]; then
            if [ $ESCALATION_COUNT -ge $MAX_ESCALATIONS ]; then
                log "${RED}$MAX_ESCALATIONS escalations hit — giving up${RESET}"
                write_status "stuck"
                echo -e "\n${RED}${BOLD}  ✗ STUCK — too many escalations.${RESET}\n"
                break
            fi

            echo -e "\n${YELLOW}${BOLD}  ⚠  Escalation(s) need fixing:${RESET}"
            for ((w=0; w<WORKERS; w++)); do
                if [ "${WORKER_EXITS[$w]}" -eq 2 ]; then
                    echo -e "  Worker $w → $LOGDIR/escalate-w${w}.md"
                fi
            done
            echo -e "  Fix the issues above, then write 'fixed' to $STATUS\n"
            write_status "needs_fix"

            WAIT_SECS=0
            MAX_WAIT=1800
            while true; do
                SVAL=$(read_status)
                if [[ "$SVAL" == "fixed" ]] || [[ "$SVAL" == fixed:* ]]; then
                    log "${GREEN}Fix confirmed — resuming${RESET}"
                    ESCALATION_COUNT=0
                    write_status "running"
                    break
                fi
                if [ $WAIT_SECS -ge $MAX_WAIT ]; then
                    log "${RED}Timed out waiting for fix — stopping${RESET}"
                    write_status "stuck"
                    break 2
                fi
                sleep 5
                WAIT_SECS=$((WAIT_SECS + 5))
            done
            continue
        fi

        # ── check for remaining work ──────────────────────────────────────────
        if more_work_remains; then
            log "${GREEN}Batch done — tasks remain; looping${RESET}"
            write_status "running"
            continue
        else
            log "${GREEN}🎉 All tasks complete!${RESET}"
            write_status "done"
            echo -e "\n${GREEN}${BOLD}  ✓ DONE — all tasks finished.${RESET}\n"
            break
        fi
    done

    log "Supervisor finished. Final status: $(read_status)"
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# ── SEQUENTIAL path (original behaviour, --workers 1) ─────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
START_AT=1
write_status "running:$START_AT"

while true; do
    log "▶  work.py --start-at $START_AT ${PASS_ARGS[*]+"${PASS_ARGS[@]}"}"
    "$PYTHON" "$SOVEREIGN/work.py" --project "$PROJECT" --start-at "$START_AT" \
        "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}" &
    WORK_PIDS=($!)
    wait "${WORK_PIDS[0]}" || true
    EXIT=$?
    WORK_PIDS=()
    log "work.py exited with code $EXIT"

    if [ $EXIT -eq 130 ]; then
        echo -e "\n  Supervisor interrupted."
        write_status "stopped"
        exit 130
    fi

    if [ $EXIT -eq 0 ]; then
        run_promote_rules
        if more_work_remains; then
            log "${GREEN}Batch done — more tasks remain; looping immediately${RESET}"
            write_status "running:$START_AT"
            START_AT=1
            ESCALATION_COUNT=0
            continue
        else
            log "${GREEN}🎉 All ROADMAP tasks complete!${RESET}"
            write_status "done"
            echo -e "\n${GREEN}${BOLD}  ✓ DONE — all tasks finished.${RESET}\n"
            break
        fi
    fi

    if [ $EXIT -eq 2 ]; then
        ESCALATION_COUNT=$((ESCALATION_COUNT + 1))
        TASK_NUM=$(grep -oP '(?<=--start-at )\d+' "$PROJECT/logs/escalate.md" 2>/dev/null \
                   || echo "$START_AT")

        if [ "$TASK_NUM" -eq "$LAST_ESCALATION_TASK" ] && [ $ESCALATION_COUNT -ge 3 ]; then
            log "${RED}Same task ($TASK_NUM) escalated 3× in a row — marking STUCK${RESET}"
            write_status "stuck:$TASK_NUM"
            echo -e "\n${RED}${BOLD}  ✗ STUCK — task $TASK_NUM escalated 3 times.${RESET}"
            break
        fi

        if [ $ESCALATION_COUNT -ge $MAX_ESCALATIONS ]; then
            log "${RED}$MAX_ESCALATIONS escalations hit — giving up${RESET}"
            write_status "stuck:$TASK_NUM"
            break
        fi

        LAST_ESCALATION_TASK="$TASK_NUM"
        log "${YELLOW}Escalation $ESCALATION_COUNT — task $TASK_NUM. Waiting for fix...${RESET}"
        write_status "needs_fix:$TASK_NUM"

        WAIT_SECS=0
        MAX_WAIT=1800
        while true; do
            SVAL=$(read_status)
            if [[ "$SVAL" == fixed:* ]]; then
                NEW_START="${SVAL#fixed:}"
                log "${GREEN}Fix confirmed — resuming at task $NEW_START${RESET}"
                START_AT="$NEW_START"
                ESCALATION_COUNT=0
                write_status "running:$START_AT"
                break
            fi
            if [ $WAIT_SECS -ge $MAX_WAIT ]; then
                log "${RED}Timed out waiting for fix${RESET}"
                write_status "stuck:$TASK_NUM"
                break 2
            fi
            sleep 5
            WAIT_SECS=$((WAIT_SECS + 5))
        done
        continue
    fi

    log "${YELLOW}Unexpected exit code $EXIT — stopping${RESET}"
    write_status "done_with_failures"
    break
done

log "Supervisor finished. Final status: $(read_status)"
