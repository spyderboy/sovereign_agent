#!/usr/bin/env bash
# sweep.sh — cheap pass, expensive pass, repeat until nothing more moves.
#
#   ./sweep.sh ~/Code/witches_bricks
#   CHEAP=qwen25-t1 STRONG=qwen25 ./sweep.sh ~/Code/witches_bricks
#   MAX_CYCLES=6 CHEAP_MAX=5400 STRONG_MAX=7200 ./sweep.sh ~/Code/witches_bricks
#
# WHY TWO PASSES AND A LOOP
#
# A roadmap is a dependency graph, not a queue. Run it top to bottom with a
# tier ladder and the ladder fires for the wrong reason: a task fails because a
# sibling it imports has not landed yet, the harness reads that as "too hard",
# and a 35B is spent on something the 14B does for free one pass later.
#
# So: sweep with the cheap model only, DEFERRING anything whose imports are
# missing (work.py's dependency guard does this — it costs one line in the log
# and no attempts). Then hand what remains to the full ladder, which is now
# looking at genuinely hard tasks rather than blocked ones. Landing those
# unblocks more, so repeat. Stop when a whole cycle changes nothing: that is
# the fixpoint, and whatever is left needs a human.
#
# Every pass is capped. A model that cannot do a task leaves it unchecked and
# the supervisor relaunches forever otherwise.

set -uo pipefail

PROJECT="${1:-$HOME/Code/witches_bricks}"
PROJECT="${PROJECT%/}"
SOVEREIGN="$(cd "$(dirname "$0")" && pwd)"
CHEAP="${CHEAP:-qwen25-t1}"
STRONG="${STRONG:-qwen25}"
CHEAP_MAX="${CHEAP_MAX:-5400}"
STRONG_MAX="${STRONG_MAX:-7200}"
MAX_CYCLES="${MAX_CYCLES:-5}"
RESULTS="$HOME/sweep-$(date +%m%d-%H%M)"

BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; DIM="\033[2m"; RESET="\033[0m"
say() { echo -e "[$(date '+%H:%M:%S')] $*"; }
mkdir -p "$RESULTS"

done_count() { grep -c '^- \[x\]' "$PROJECT/ROADMAP.md" 2>/dev/null || echo 0; }
open_count() { grep -c '^- \[ \]' "$PROJECT/ROADMAP.md" 2>/dev/null || echo 0; }

reset_tree() {
    pkill -9 -f supervisor.sh 2>/dev/null
    pkill -9 -f work.py 2>/dev/null
    sleep 2
    rm -f "$PROJECT/logs/run.lock"
    # UNCOMMITTED WORK IS NOT DEBRIS. `git checkout -f` below cannot tell a
    # half-finished worker branch from an hour of hand-tuned config, and on
    # 2026-08-13 it silently destroyed the second: guard-hint fixes, a context
    # change and a new preflight check, all gone at launch, so the run came back
    # up with exactly the broken config it had just been stopped to fix.
    # Refusing is always right here — a human edit is never something this
    # script should be throwing away on its own initiative.
    #
    # STRICT ONLY ON THE FIRST CALL. run_pass() calls reset_tree before every
    # pass, and by then the dirty files are the previous pass's abandoned
    # worker output — which is precisely what a reset exists to clear. Refusing
    # there killed the 17:07 run at its first pass boundary, 30 minutes in,
    # with no error the user would ever see. Only the launch-time call, before
    # any worker has run, can distinguish a human edit from debris.
    local pre
    if [ "${1:-}" = "strict" ]; then
    pre=$(cd "$PROJECT" && git status --porcelain \
          | grep -v '^?? logs/' | grep -v '^ M ROADMAP.md' || true)
    fi
    if [ -n "${pre:-}" ]; then
        echo -e "${RED}✗ refusing to start: $PROJECT has uncommitted changes${RESET}"
        echo "$pre"
        echo -e "${DIM}  Commit them, or discard them deliberately:"
        echo -e "    git -C $PROJECT add -A && git -C $PROJECT commit -m '...'"
        echo -e "    git -C $PROJECT checkout -f main && git -C $PROJECT clean -fd${RESET}"
        exit 1
    fi
    ( cd "$PROJECT" \
      && find .git -name '*.lock' -delete \
      && git checkout -f main \
      && git clean -fd \
      && git for-each-ref --format='%(refname:short)' 'refs/heads/task-*' \
         | xargs -r git branch -D ) >>"$RESULTS/reset.log" 2>&1
    local dirty
    dirty=$(cd "$PROJECT" && git status --porcelain | grep -vc '^?? logs/' || true)
    if [ "${dirty:-0}" != "0" ]; then
        echo -e "${RED}✗ tree still dirty after reset — stopping${RESET}"
        ( cd "$PROJECT" && git status --short ); exit 1
    fi
}

run_pass() {   # $1 profile  $2 cap  $3 label
    local prof="$1" cap="$2" label="$3" log="$RESULTS/$3.log"
    local before after start elapsed killed=0
    before=$(done_count)
    say "${BOLD}$label${RESET}  profile=$prof  cap=${cap}s  done=$before"
    reset_tree

    start=$SECONDS
    ( cd "$SOVEREIGN" && SOVEREIGN_PROFILE="$prof" \
        ./supervisor.sh "$PROJECT" --features-only >>"$log" 2>&1 ) &
    local sup=$!
    while kill -0 "$sup" 2>/dev/null; do
        if [ $((SECONDS - start)) -ge "$cap" ]; then
            say "${YELLOW}cap reached — stopping $label${RESET}"
            pkill -9 -f supervisor.sh 2>/dev/null
            pkill -9 -f work.py 2>/dev/null
            killed=1; break
        fi
        sleep 20
    done
    wait "$sup" 2>/dev/null
    elapsed=$((SECONDS - start)); after=$(done_count)
    local note=""; [ "$killed" = "1" ] && note=" (hit cap)"
    say "${GREEN}$label: $before → $after in ${elapsed}s${note}${RESET}"
    PASS_GAIN=$((after - before))
}

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Sweep${RESET}   $PROJECT"
echo -e "  cheap  : $CHEAP   cap ${CHEAP_MAX}s"
echo -e "  strong : $STRONG  cap ${STRONG_MAX}s"
echo -e "  cycles : up to $MAX_CYCLES, stopping when one changes nothing"
echo -e "  logs   : $RESULTS"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"

[ -d "$PROJECT/.git" ] || { echo -e "${RED}✗ not a git repo${RESET}"; exit 1; }
for p in "$CHEAP" "$STRONG"; do
    [ -f "$SOVEREIGN/profiles/$p.toml" ] || {
        echo -e "${RED}✗ no profile '$p' — have: $(ls "$SOVEREIGN/profiles" | tr '\n' ' ')${RESET}"
        exit 1; }
done
if [ -f "$PROJECT/tool/preflight.py" ]; then
    if ! ( cd "$PROJECT" && SOVEREIGN_DIR="$SOVEREIGN" python3 tool/preflight.py \
           >"$RESULTS/preflight.log" 2>&1 ); then
        tail -20 "$RESULTS/preflight.log"
        echo -e "${RED}✗ preflight failed — fix this first; no model can pass these${RESET}"
        exit 1
    fi
    say "preflight clean"
fi

# The one moment a dirty tree means a HUMAN edit rather than worker debris:
# before any worker has run. Every later reset_tree is deliberately permissive.
reset_tree strict

START_DONE=$(done_count)
SUMMARY=""
for cycle in $(seq 1 "$MAX_CYCLES"); do
    echo -e "\n${BOLD}──────── cycle $cycle ────────${RESET}"
    CYCLE_BEFORE=$(done_count)

    run_pass "$CHEAP"  "$CHEAP_MAX"  "cycle${cycle}-cheap"
    CHEAP_GAIN=$PASS_GAIN

    if [ "$(open_count)" = "0" ]; then
        say "${GREEN}roadmap complete${RESET}"
        SUMMARY+="  cycle $cycle: cheap +$CHEAP_GAIN, done\n"
        break
    fi

    run_pass "$STRONG" "$STRONG_MAX" "cycle${cycle}-strong"
    STRONG_GAIN=$PASS_GAIN

    CYCLE_GAIN=$(( $(done_count) - CYCLE_BEFORE ))
    SUMMARY+="  cycle $cycle: cheap +$CHEAP_GAIN, strong +$STRONG_GAIN, total +$CYCLE_GAIN\n"
    say "cycle $cycle gained $CYCLE_GAIN task(s)"

    if [ "$CYCLE_GAIN" = "0" ]; then
        say "${YELLOW}a full cycle changed nothing — fixpoint reached${RESET}"
        break
    fi
    if [ "$(open_count)" = "0" ]; then
        say "${GREEN}roadmap complete${RESET}"; break
    fi
done

reset_tree
echo
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Sweep finished${RESET}  $START_DONE → $(done_count) of $(grep -cE '^- \[[ x]\]' "$PROJECT/ROADMAP.md")"
echo -e "$SUMMARY"
REMAIN=$(open_count)
if [ "$REMAIN" != "0" ]; then
    echo -e "${YELLOW}  $REMAIN task(s) still open:${RESET}"
    grep -oE '^- \[ \] In (\S+):' "$PROJECT/ROADMAP.md" | sed 's/- \[ \] In //;s/://' | sed 's/^/     /'
    echo -e "${DIM}  Anything still here after a fixpoint is not a model problem —"
    echo -e "  check its gate, its dependencies, and whether the task text names"
    echo -e "  every signature it tells the worker to call.${RESET}"
fi
[ -f "$PROJECT/logs/needs_review.md" ] && \
    echo -e "${DIM}  parked for review: $PROJECT/logs/needs_review.md${RESET}"
echo -e "${DIM}  logs: $RESULTS${RESET}"
