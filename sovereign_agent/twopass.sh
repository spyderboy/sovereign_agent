#!/usr/bin/env bash
# twopass.sh — run the roadmap with one model, then send whatever is still
# unfinished to a second model. Unattended-safe.
#
#   ./twopass.sh ~/Code/witches_bricks
#   PASS1=qwen25 PASS2=gemma26 ./twopass.sh ~/Code/witches_bricks
#   PASS1_MAX=21600 PASS2_MAX=10800 ./twopass.sh ~/Code/witches_bricks
#
# Pass 2 needs no special "retry" mode: a finished task is checked off in
# ROADMAP.md, so whatever remains unchecked IS the failed set. Pointing a
# different profile at the same project picks up exactly those.
#
# WHY A WRAPPER AND NOT TWO TERMINALS
#
# Between passes the tree has to be returned to a clean state — killed workers,
# no leftover task-* branch, no untracked file from an aborted task. Skipping
# that is what produces the "work.py exited, looping immediately" wall, and it
# is the single most common way an unattended run wastes a night doing nothing.
#
# EVERY PASS IS CAPPED. A model that cannot do a task leaves it unchecked, the
# supervisor sees work remaining and relaunches, and that repeats until
# something stops it. PASS1_MAX / PASS2_MAX are that something.

set -uo pipefail

PROJECT="${1:-$HOME/Code/witches_bricks}"
PROJECT="${PROJECT%/}"
SOVEREIGN="$(cd "$(dirname "$0")" && pwd)"
PASS1="${PASS1:-qwen25}"
PASS2="${PASS2:-gemma26}"
PASS1_MAX="${PASS1_MAX:-28800}"      # 8h
PASS2_MAX="${PASS2_MAX:-14400}"      # 4h
RESULTS="$HOME/twopass-$(date +%m%d-%H%M)"

BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; DIM="\033[2m"; RESET="\033[0m"
say() { echo -e "[$(date '+%H:%M:%S')] $*"; }
mkdir -p "$RESULTS"

progress() {   # done / total
    local d t
    d=$(grep -c '^- \[x\]' "$PROJECT/ROADMAP.md" 2>/dev/null || echo 0)
    t=$(grep -cE '^- \[[ x]\]' "$PROJECT/ROADMAP.md" 2>/dev/null || echo 0)
    echo "$d/$t"
}

reset_tree() {
    pkill -9 -f supervisor.sh 2>/dev/null
    pkill -9 -f work.py 2>/dev/null
    sleep 2
    ( cd "$PROJECT" \
      && find .git -name '*.lock' -delete \
      && git checkout -f main \
      && git clean -fd \
      && git for-each-ref --format='%(refname:short)' 'refs/heads/task-*' \
         | xargs -r git branch -D ) >>"$RESULTS/reset.log" 2>&1
    local dirty
    dirty=$(cd "$PROJECT" && git status --porcelain | grep -v '^?? logs/' | wc -l | tr -d ' ')
    if [ "$dirty" != "0" ]; then
        echo -e "${RED}✗ tree still dirty after reset — stopping${RESET}"
        ( cd "$PROJECT" && git status --short )
        exit 1
    fi
}

run_pass() {   # $1 profile  $2 cap seconds  $3 label
    local prof="$1" cap="$2" label="$3" logf="$RESULTS/$3.log"
    local before after start elapsed killed=0

    before=$(progress)
    say "${BOLD}$label${RESET} — profile '$prof', cap ${cap}s, from $before"
    reset_tree

    start=$SECONDS
    ( cd "$SOVEREIGN" && SOVEREIGN_PROFILE="$prof" \
        ./supervisor.sh "$PROJECT" --features-only >>"$logf" 2>&1 ) &
    local sup=$!
    while kill -0 "$sup" 2>/dev/null; do
        if [ $((SECONDS - start)) -ge "$cap" ]; then
            say "${YELLOW}cap reached — stopping $label${RESET}"
            pkill -9 -f supervisor.sh 2>/dev/null
            pkill -9 -f work.py 2>/dev/null
            killed=1; break
        fi
        sleep 30
    done
    wait "$sup" 2>/dev/null
    elapsed=$((SECONDS - start))
    after=$(progress)

    local note=""; [ "$killed" = "1" ] && note=" (hit cap)"
    say "${GREEN}$label done: $before → $after in ${elapsed}s${note}${RESET}"
    SUMMARY+="  $label  $prof  $before → $after  ${elapsed}s${note}\n"
}

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Two-pass run${RESET}"
echo -e "  project : $PROJECT"
echo -e "  pass 1  : $PASS1   cap ${PASS1_MAX}s"
echo -e "  pass 2  : $PASS2   cap ${PASS2_MAX}s  (whatever pass 1 left unfinished)"
echo -e "  logs    : $RESULTS"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"

[ -d "$PROJECT/.git" ] || { echo -e "${RED}✗ $PROJECT is not a git repo${RESET}"; exit 1; }
command -v ollama >/dev/null || { echo -e "${RED}✗ ollama not on PATH${RESET}"; exit 1; }
OLLAMA_LIST="$(ollama list 2>&1)"
if [ $? -ne 0 ] || [ "$(printf '%s\n' "$OLLAMA_LIST" | grep -c .)" -lt 2 ]; then
    echo -e "${RED}✗ ollama is not responding — nothing can run${RESET}"
    echo -e "  it said: $(printf '%s' "$OLLAMA_LIST" | head -1)"
    echo -e "\n  start it, then rerun:  ${BOLD}nohup ollama serve > /tmp/ollama.log 2>&1 &${RESET}"
    exit 1
fi
for p in "$PASS1" "$PASS2"; do
    [ -f "$SOVEREIGN/profiles/$p.toml" ] || {
        echo -e "${RED}✗ profiles/$p.toml not found${RESET}"
        echo -e "  have: $(ls "$SOVEREIGN/profiles" 2>/dev/null | tr '\n' ' ')"; exit 1; }
done
say "preflight ok — $(printf '%s\n' "$OLLAMA_LIST" | tail -n +2 | grep -c .) models available\n"

SUMMARY=""
START_ALL=$(progress)

run_pass "$PASS1" "$PASS1_MAX" "pass1"

REMAIN=$(grep -c '^- \[ \]' "$PROJECT/ROADMAP.md" 2>/dev/null || echo 0)
if [ "$REMAIN" = "0" ]; then
    say "${GREEN}nothing left after pass 1 — skipping pass 2${RESET}"
    SUMMARY+="  pass2  skipped (roadmap complete)\n"
else
    say "$REMAIN task(s) still unfinished — handing them to $PASS2"
    run_pass "$PASS2" "$PASS2_MAX" "pass2"
fi

reset_tree

echo
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Summary${RESET}   started at $START_ALL, finished at $(progress)"
echo -e "$SUMMARY"
if [ -f "$PROJECT/logs/needs_review.md" ]; then
    echo -e "${YELLOW}  ⏸  parked for review: $(grep -c '^## ' "$PROJECT/logs/needs_review.md") task(s)${RESET}"
    echo -e "${DIM}     $PROJECT/logs/needs_review.md — each names the file actually at fault${RESET}"
fi
echo -e "${DIM}  still unfinished: $(grep -c '^- \[ \]' "$PROJECT/ROADMAP.md") task(s)${RESET}"
echo -e "${DIM}  logs: $RESULTS${RESET}"
