#!/usr/bin/env bash
# bakeoff.sh — run several models over the SAME band of tasks, one after another,
# then print a side-by-side comparison.
#
#   ./bakeoff.sh ~/Code/witches_bricks
#   BAND=spawnRules.dart,moveRules.dart ./bakeoff.sh ~/Code/witches_bricks
#   MAX_ARM_S=3600 ./bakeoff.sh ~/Code/witches_bricks
#
# Each arm is an isolated clone with the band's tasks rewound and everything
# after them parked, so an arm ends on its own. The incumbent needs no arm —
# its numbers are already in the source project's logs.
#
# SEQUENTIAL ON PURPOSE. Two models resident at once contend for RAM, and on a
# 32 GB machine that contention once turned a 597s attempt into 5257s. Running
# these in parallel would produce timings that mean nothing.
#
# THE WATCHDOG IS NOT OPTIONAL. When a task is skipped it stays unchecked, the
# supervisor sees work remaining and relaunches — so a model that simply cannot
# do a task will loop until something stops it. MAX_ARM_S is that something.

set -uo pipefail

PROJECT="${1:-$HOME/Code/witches_bricks}"
PROJECT="${PROJECT%/}"
SOVEREIGN="$(cd "$(dirname "$0")" && pwd)"
BAND="${BAND:-spawnRules.dart,moveRules.dart,leapRules.dart,brewRules.dart}"
MAX_ARM_S="${MAX_ARM_S:-5400}"          # 90 min per arm
RESULTS="$HOME/bakeoff-$(date +%m%d-%H%M)"

# model | params(B) | tag
ARMS=(
  "gemma4:26b|26|g26"
  "qwen2.5-coder:32b|32|q32"
  "qwen3.6:35b-a3b-coding-nvfp4|35|q35"
)

BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; DIM="\033[2m"; RESET="\033[0m"
say() { echo -e "[$(date '+%H:%M:%S')] $*"; }

mkdir -p "$RESULTS"

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  🥊  Model bake-off${RESET}"
echo -e "  project : $PROJECT"
echo -e "  band    : $BAND"
echo -e "  arms    : ${#ARMS[@]}   cap ${MAX_ARM_S}s each"
echo -e "  results : $RESULTS"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"

# ── Preconditions ─────────────────────────────────────────────────────────────
if [ ! -d "$PROJECT/.git" ]; then
    echo -e "${RED}✗ $PROJECT is not a git repo${RESET}"; exit 1
fi
if ! command -v ollama >/dev/null; then
    echo -e "${RED}✗ ollama not on PATH${RESET}"; exit 1
fi

# Ask the SERVER, once, and fail loudly. The first version checked each model
# against `ollama list` without checking that the list came back at all — so a
# stopped server produced three cheerful "model not installed" skips and an
# empty bake-off. An empty answer to "what do you have" is not "nothing".
OLLAMA_LIST="$(ollama list 2>&1)"
if [ $? -ne 0 ] || [ "$(printf '%s\n' "$OLLAMA_LIST" | grep -c .)" -lt 2 ]; then
    echo -e "${RED}✗ ollama is not responding — no models can be checked or run${RESET}"
    echo -e "  it said: $(printf '%s' "$OLLAMA_LIST" | head -1)"
    echo -e "\n  start it in another terminal, then rerun:"
    echo -e "    ${BOLD}ollama serve${RESET}"
    echo -e "  or in the background:"
    echo -e "    ${BOLD}nohup ollama serve > /tmp/ollama.log 2>&1 &${RESET}"
    exit 1
fi
say "ollama has $(printf '%s\n' "$OLLAMA_LIST" | tail -n +2 | grep -c .) model(s)"

say "stopping anything already running"
pkill -9 -f supervisor.sh 2>/dev/null
pkill -9 -f work.py 2>/dev/null
sleep 1
LEFT=$(ps aux | grep -E '[w]ork.py|[s]upervisor.sh' | wc -l | tr -d ' ')
if [ "$LEFT" != "0" ]; then
    echo -e "${RED}✗ $LEFT process(es) still alive — stop them and rerun${RESET}"; exit 1
fi

# Verify every band entry is actually a completed task, before cloning anything.
say "checking the band against $PROJECT/ROADMAP.md"
MISSING=$(BAND="$BAND" python3 - "$PROJECT" <<'PY'
import os, re, sys
band = [b.strip() for b in os.environ["BAND"].split(",") if b.strip()]
done = [l for l in open(os.path.join(sys.argv[1], "ROADMAP.md")) if l.startswith("- [x]")]
blob = "".join(done)
print(",".join(b for b in band if b not in blob))
PY
)
if [ -n "$MISSING" ]; then
    echo -e "${RED}✗ not completed tasks in the source project: $MISSING${RESET}"
    echo -e "  the band must be tasks that ALREADY ran, so a baseline exists"
    exit 1
fi
echo -e "${GREEN}  ✓ band verified${RESET}\n"

# ── Run each arm ──────────────────────────────────────────────────────────────
SUMMARY=""
for spec in "${ARMS[@]}"; do
    IFS='|' read -r MODEL PARAMS TAG <<< "$spec"
    CLONE="${PROJECT}_ab_${TAG}"
    LOGF="$RESULTS/$TAG.log"

    echo -e "${BOLD}── arm: $MODEL  (tag $TAG) ──${RESET}"

    if ! printf '%s\n' "$OLLAMA_LIST" | awk '{print $1}' | grep -qxF "$MODEL"; then
        echo -e "${YELLOW}  ⚠ $MODEL not installed — skipping. Have: $(printf '%s\n' "$OLLAMA_LIST" | awk 'NR>1{print $1}' | tr '\n' ' ')${RESET}\n"
        SUMMARY+="  $TAG  SKIPPED (model not installed)\n"
        continue
    fi

    # Free whatever is resident so each arm starts from the same cold state.
    for other in "${ARMS[@]}"; do
        IFS='|' read -r m _ _ <<< "$other"; ollama stop "$m" >/dev/null 2>&1
    done

    if [ -d "$CLONE" ]; then
        case "$CLONE" in
            *_ab_"$TAG") say "removing previous arm at $CLONE"; rm -rf "$CLONE" ;;
            *) echo -e "${RED}✗ refusing to remove $CLONE${RESET}"; exit 1 ;;
        esac
    fi
    rm -rf "$HOME/ab-$TAG"

    say "building arm"
    if ! python3 "$SOVEREIGN/setup_ab.py" "$PROJECT" --model "$MODEL" \
            --params "$PARAMS" --tag "$TAG" --band "$BAND" >>"$LOGF" 2>&1; then
        echo -e "${RED}  ✗ setup_ab failed — see $LOGF${RESET}\n"
        SUMMARY+="  $TAG  FAILED (setup)\n"; continue
    fi

    say "flutter pub get"
    ( cd "$CLONE" && flutter pub get >>"$LOGF" 2>&1 )

    say "running — capped at ${MAX_ARM_S}s, output to $LOGF"
    START=$SECONDS
    ( cd "$SOVEREIGN" && SOVEREIGN_PROFILE="$TAG" \
        ./supervisor.sh "$CLONE" --features-only >>"$LOGF" 2>&1 ) &
    SUP=$!

    # Watchdog: poll rather than `timeout`, which macOS lacks by default.
    KILLED=0
    while kill -0 "$SUP" 2>/dev/null; do
        if [ $((SECONDS - START)) -ge "$MAX_ARM_S" ]; then
            echo -e "${YELLOW}  ⚠ hit the ${MAX_ARM_S}s cap — stopping this arm${RESET}"
            pkill -9 -f supervisor.sh 2>/dev/null; pkill -9 -f work.py 2>/dev/null
            KILLED=1; break
        fi
        sleep 10
    done
    wait "$SUP" 2>/dev/null
    ELAPSED=$((SECONDS - START))

    DONE=$(grep -c '^- \[x\] In .*\(spawnRules\|moveRules\|leapRules\|brewRules\)' \
           "$CLONE/ROADMAP.md" 2>/dev/null || echo 0)
    NBAND=$(echo "$BAND" | tr ',' '\n' | grep -c . )
    NOTE=""; [ "$KILLED" = "1" ] && NOTE=" (hit cap)"
    echo -e "${GREEN}  ✓ arm finished: ${DONE}/${NBAND} band tasks landed in ${ELAPSED}s${NOTE}${RESET}\n"
    SUMMARY+="  $TAG  ${DONE}/${NBAND} tasks  ${ELAPSED}s${NOTE}\n"

    ollama stop "$MODEL" >/dev/null 2>&1
done

# ── Compare ───────────────────────────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Arms${RESET}"
echo -e "$SUMMARY"

CMP=( python3 "$SOVEREIGN/ab_compare.py" )
FIRST_TAG=""
for spec in "${ARMS[@]}"; do
    IFS='|' read -r _ _ TAG <<< "$spec"
    [ -z "$FIRST_TAG" ] && [ -d "$HOME/ab-$TAG/baseline" ] && FIRST_TAG="$TAG"
    [ -d "${PROJECT}_ab_${TAG}/logs" ] && CMP+=( --arm "${TAG}=${PROJECT}_ab_${TAG}/logs" )
done
[ -n "$FIRST_TAG" ] && CMP=( "${CMP[@]:0:2}" --arm "incumbent=$HOME/ab-$FIRST_TAG/baseline" "${CMP[@]:2}" )

echo -e "${BOLD}  Comparison${RESET}"
"${CMP[@]}" 2>&1 | tee "$RESULTS/compare.txt"

echo
echo -e "${DIM}  logs and comparison: $RESULTS${RESET}"
echo -e "${DIM}  read ATTEMPTS, not wall time — and remember tier 2+ numbers are"
echo -e "  survivorship-biased, since those models only see tasks that already failed.${RESET}"
echo -e "${DIM}  clean up when done:  rm -rf ${PROJECT}_ab_* ~/ab-*${RESET}"
