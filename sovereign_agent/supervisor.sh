#!/usr/bin/env bash
# supervisor.sh — Run work.py in a loop; supports parallel workers.
#
# Usage (sequential, original behaviour):
#   ./supervisor.sh ~/Code/galaxican
#   ./supervisor.sh ~/Code/galaxican --features-only
#
# Usage (parallel — N workers share the task list by stride):
#   ./supervisor.sh ~/Code/galaxican --workers 4
#   ./supervisor.sh ~/Code/galaxican --workers 4 --features-only
#
# Usage (quick/deep split):
#   IMPORTANT: --quick SKIPS TIER 1 — it starts at tier_idx=1 (currently just
#   TIER2_MODEL, gemma4:26b, since Tier3 at 35B exceeds the <30B quick-mode
#   param cap). It is NOT a tier-1 sweep despite the name. Racing
#   (RACE_MODEL/RACE_ENABLED) only ever fires at tier_idx==0 (tier 1), so
#   --quick never exercises it — run WITHOUT --quick/--deep to hit tier 1
#   and trigger a race. See work.py's `run_start_tier_idx = 1 if args.quick
#   else 0` for the source of truth.
#   ./supervisor.sh ~/Code/galaxican --quick              # tier-2 only (<30B); failures → tier2_queue.jsonl
#   ./supervisor.sh ~/Code/galaxican --deep               # tiers 2-3 on queued failures only
#   ./supervisor.sh ~/Code/galaxican --workers 4 --quick  # parallel quick sweep
#   ./supervisor.sh ~/Code/galaxican --workers 2 --deep   # parallel deep mop-up
#
# Usage (full run — quick sweep then automatic deep mop-up in one command):
#   ./supervisor.sh ~/Code/galaxican --full               # 4-worker quick, then 1-worker deep
#   ./supervisor.sh ~/Code/galaxican --full --workers 6   # custom worker count for quick pass
#
#   RULE: feature tasks and test tasks are NEVER run in the same worker
#   session. There's features, and there's tests — mixing them in one
#   session lets a test run against a feature implementation that hasn't
#   landed yet (or that a different parallel worker is still mid-edit on).
#   --full enforces this automatically: it runs a complete features-only
#   session (quick + deep mop-up) to completion, THEN a complete tests-only
#   session — never interleaved. See sovereign_agent/CLAUDE.md's "General
#   Rules" section for the source of truth on this rule.
#
#   If you invoke work.py or supervisor.sh directly WITHOUT --full, always
#   pass --features-only or --tests-only yourself:
#     ./supervisor.sh ~/Code/galaxican --features-only
#     ./supervisor.sh ~/Code/galaxican --tests-only
#   Running with neither flag (and without --full) mixes both task types in
#   one session — supervisor.sh will print a warning if you do this.
#
# Each worker K handles tasks at positions K, K+N, K+2N … (round-robin),
# grouped into dependency-respecting chains first — see work.py's
# chain-building comment above the stride-assignment code — so a same-file
# run or an implement→test pair always lands on a single worker.
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

# ── parse --workers N and --full from args ────────────────────────────────────
WORKERS=1
FULL_RUN=false
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
        --full)
            FULL_RUN=true
            shift
            ;;
        *)
            PASS_ARGS+=("$1")
            shift
            ;;
    esac
done

# ── --full: run a features session, then a tests session — never mixed ───────
# See the "RULE" note in the usage header: feature tasks and test tasks must
# never be in the same worker session, so this does NOT run one quick+deep
# pass over everything. It runs a complete features-only session (quick +
# deep mop-up) to completion, then a complete tests-only session — two fully
# independent supervisor loops, each with its own quick sweep and deep
# mop-up, composed one after the other.
if $FULL_RUN; then
    # Quick workers from --workers flag; deep workers from DEEP_WORKERS env (default 2)
    QUICK_WORKERS="${WORKERS:-3}"
    DEEP_WORKERS="${DEEP_WORKERS:-2}"
    log() { echo -e "[$(date '+%H:%M:%S')] $*"; }

    echo -e "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  🤖  Full Run — Features session → Tests session${RESET}"
    echo -e "${BOLD}  Project : $PROJECT${RESET}"
    echo -e "${BOLD}  Quick   : $QUICK_WORKERS workers${RESET}"
    echo -e "${BOLD}  Deep    : $DEEP_WORKERS workers${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  Never mixed: features run to completion first, then tests.\n"

    # $1 = mode ("features"/"tests"), $2 = mode flag, $3 = human label
    run_full_session() {
        local mode="$1" mode_flag="$2" label="$3"
        echo -e "${BOLD}  ── ${label} session ──${RESET}"

        echo -e "${BOLD}  Pass 1/2 — Quick sweep ($QUICK_WORKERS workers)${RESET}"
        "$0" "$PROJECT" --workers "$QUICK_WORKERS" --quick "$mode_flag" "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"
        local exit1=$?
        if [[ $exit1 -eq 130 ]]; then
            echo -e "\n  Full run interrupted during ${label} quick pass."
            exit 130
        fi

        local queued
        queued=$("$PYTHON" "$SOVEREIGN/work.py" --project "$PROJECT" --queue-remaining-count "$mode" 2>/dev/null)
        if [[ -n "$queued" ]] && [[ "$queued" -gt 0 ]]; then
            echo -e "\n${BOLD}  Pass 2/2 — Deep mop-up ($queued $mode failures, $DEEP_WORKERS workers)${RESET}"
            "$0" "$PROJECT" --workers "$DEEP_WORKERS" --deep "$mode_flag" "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"
            local exit2=$?
            if [[ $exit2 -eq 130 ]]; then
                echo -e "\n  Full run interrupted during ${label} deep pass."
                exit 130
            fi
        else
            echo -e "\n${GREEN}${BOLD}  ✓ No $mode failures queued — deep pass not needed.${RESET}"
        fi
    }

    run_full_session "features" "--features-only" "Features"
    echo ""
    run_full_session "tests" "--tests-only" "Tests"

    echo -e "\n${GREEN}${BOLD}  ✓ Full run complete — features then tests, never mixed.${RESET}\n"
    exit 0
fi

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

# Resolves to "features", "tests", or "all" based on this invocation's
# PASS_ARGS. Used to keep has_unchecked_tasks/has_queued_tasks honest about
# which task type they're checking, so a --tests-only loop never keeps
# spinning on unchecked feature tasks it's not allowed to touch (and vice
# versa) — see the never-mix-features-and-tests rule in the header above.
resolve_task_mode() {
    for arg in "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"; do
        if [ "$arg" = "--features-only" ]; then echo "features"; return; fi
        if [ "$arg" = "--tests-only" ]; then echo "tests"; return; fi
    done
    echo "all"
}

# Delegates to work.py's --remaining-count / --queue-remaining-count so the
# exact same parse_all_tasks()/_is_test_task() classification is used here
# as in the real run — no separate regex to keep in sync in bash.
has_unchecked_tasks() {
    local mode count
    mode=$(resolve_task_mode)
    count=$("$PYTHON" "$SOVEREIGN/work.py" --project "$PROJECT" --remaining-count "$mode" 2>/dev/null)
    [ -n "$count" ] && [ "$count" -gt 0 ]
}

# In --deep mode the workers only drain tier2_queue.jsonl, not ROADMAP.md.
# Use this check instead of has_unchecked_tasks when running --deep.
has_queued_tasks() {
    local mode count
    mode=$(resolve_task_mode)
    count=$("$PYTHON" "$SOVEREIGN/work.py" --project "$PROJECT" --queue-remaining-count "$mode" 2>/dev/null)
    [ -n "$count" ] && [ "$count" -gt 0 ]
}

is_deep_mode() {
    for arg in "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"; do
        [ "$arg" = "--deep" ] && return 0
    done
    return 1
}

is_mode_scoped() {
    for arg in "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"; do
        [ "$arg" = "--features-only" ] && return 0
        [ "$arg" = "--tests-only" ] && return 0
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
FAST_EXITS=0

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

if ! is_mode_scoped; then
    echo -e "${YELLOW}⚠  No --features-only / --tests-only given — this session may run"
    echo -e "   feature tasks and test tasks together. Prefer ./supervisor.sh $PROJECT --full"
    echo -e "   (runs a features session, then a tests session, never mixed), or pass"
    echo -e "   --features-only / --tests-only explicitly.${RESET}\n"
fi

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
            # Same trap as the sequential path: `wait || true` reports true's
            # status, so every worker looked like a clean exit.
            if wait "${WORK_PIDS[$w]}"; then WORKER_EXITS[$w]=0; else WORKER_EXITS[$w]=$?; fi
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
    ITER_START=$SECONDS
    # `wait ... || true` sets $? to true's status, so EXIT was ALWAYS 0 and every
    # hard refusal from work.py — dirty tree, wrong branch, leftover task branch
    # — was read below as "batch finished cleanly", relaunching instantly forever.
    if wait "${WORK_PIDS[0]}"; then EXIT=0; else EXIT=$?; fi
    ITER_SECS=$((SECONDS - ITER_START))
    WORK_PIDS=()
    log "work.py exited with code $EXIT"

    # A precondition refusal returns in under a second and changes nothing, so
    # looping cannot help — only an operator can. Three in a row is a stop.
    if [ "$ITER_SECS" -lt 10 ]; then
        FAST_EXITS=$((FAST_EXITS + 1))
    else
        FAST_EXITS=0
    fi
    if [ "$FAST_EXITS" -ge 3 ]; then
        log "${RED}work.py returned in <10s three times running — refusing to spin${RESET}"
        write_status "stuck:$START_AT"
        echo -e "\n${RED}${BOLD}  ✗ STOPPED — work.py is failing a precondition, not working.${RESET}"
        echo -e "  The reason is the FIRST line of its output above, not the last."
        echo -e "  Most often: uncommitted or untracked files, or a leftover task-* branch.\n"
        echo -e "    cd $PROJECT && git status --short && git branch\n"
        break
    fi

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
