#!/usr/bin/env bash
# start.sh — one command to launch a run correctly, and show you it started.
#
#   ./start.sh ~/Code/witches_bricks
#   ./start.sh ~/Code/witches_bricks --force        # kill a live run first
#   PROFILE=gemma26 ./start.sh ~/Code/witches_bricks
#   WAIT=90 ./start.sh ~/Code/witches_bricks        # watch longer before detaching
#
# Every launch needs the same six things and forgetting any one of them has
# cost a night: no live worker, tree on main, no leftover task branch, no stale
# lock, preflight green, and the machine kept awake. Doing them by hand means
# doing five of six.
#
# It prints the startup banner before detaching, so a run that dies on the
# branch guard or a bad profile tells you NOW rather than the next time you
# think to tail the log.

set -uo pipefail

PROJECT="${1:-}"
[ -n "$PROJECT" ] && shift
PROJECT="${PROJECT:-$HOME/Code/witches_bricks}"
PROJECT="${PROJECT%/}"
SOVEREIGN="$(cd "$(dirname "$0")" && pwd)"
PROFILE="${PROFILE:-qwen25}"
WAIT="${WAIT:-45}"
FORCE=0
for a in "$@"; do [ "$a" = "--force" ] && FORCE=1; done

BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; DIM="\033[2m"; RESET="\033[0m"
die() { echo -e "${RED}✗ $*${RESET}"; exit 1; }

[ -d "$PROJECT/.git" ] || die "$PROJECT is not a git repo"
[ -f "$SOVEREIGN/profiles/$PROFILE.toml" ] || \
    die "no profile '$PROFILE' — have: $(ls "$SOVEREIGN/profiles" | tr '\n' ' ')"

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Starting a run${RESET}"
echo -e "  project : $PROJECT"
echo -e "  profile : $PROFILE"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"

LIVE=$(pgrep -f "[w]ork\.py" | wc -l | tr -d ' ')
if [ "$LIVE" != "0" ]; then
    if [ "$FORCE" = "1" ]; then
        echo "  stopping $LIVE live worker(s)"
        pkill -9 -f supervisor.sh 2>/dev/null
        pkill -9 -f work.py 2>/dev/null
        sleep 2
    else
        die "$LIVE work.py already running. Two workers on one repo deadlock on the git index.
  Re-run with --force, or stop them yourself:
    pkill -9 -f supervisor.sh; pkill -9 -f work.py"
    fi
fi

echo "  resetting the tree"
cd "$PROJECT" || die "cannot cd to $PROJECT"
find .git -name '*.lock' -delete 2>/dev/null
rm -f logs/run.lock
git checkout -f main >/dev/null 2>&1 || die "cannot check out main"
git clean -fd >/dev/null 2>&1
# Tag anything on a task branch that main does not already have, THEN delete.
# `git branch -D` discards unmerged commits silently, and a task branch is
# exactly where a hand edit lands if you commit while a run is live — that is
# how a ROADMAP fix became a dangling commit (2026-08-12). Tagging costs
# nothing and makes the mistake recoverable with `git cherry-pick`.
for b in $(git for-each-ref --format='%(refname:short)' 'refs/heads/task-*'); do
    UNMERGED=$(git rev-list --count "main..$b" 2>/dev/null || echo 0)
    if [ "${UNMERGED:-0}" != "0" ]; then
        TAG="abandoned/$(date +%m%d-%H%M)/$b"
        git tag -f "$TAG" "$b" >/dev/null 2>&1
        echo -e "${YELLOW}  ⚠ $b had $UNMERGED unmerged commit(s) — kept as tag $TAG${RESET}"
        echo -e "${DIM}     recover with: git cherry-pick $TAG${RESET}"
    fi
    git branch -D "$b" >/dev/null 2>&1
done

DIRTY=$(git status --porcelain | grep -vc '^?? logs/' || true)
[ "${DIRTY:-0}" = "0" ] || { git status --short; die "tree still dirty after reset"; }
BRANCH=$(git branch --show-current)
[ "$BRANCH" = "main" ] || die "on '$BRANCH', not main"
echo -e "${GREEN}  ✓ main, clean, no task branches${RESET}"

if [ -f tool/preflight.py ]; then
    echo "  preflight"
    if ! SOVEREIGN_DIR="$SOVEREIGN" python3 tool/preflight.py > /tmp/preflight.out 2>&1; then
        tail -20 /tmp/preflight.out
        die "preflight failed — those tasks cannot pass no matter which model runs them"
    fi
    echo -e "${GREEN}  ✓ $(tail -1 /tmp/preflight.out)${RESET}"
fi

DONE=$(grep -c '^- \[x\]' ROADMAP.md 2>/dev/null || echo 0)
ALL=$(grep -cE '^- \[[ x]\]' ROADMAP.md 2>/dev/null || echo 0)
echo -e "  roadmap : $DONE/$ALL done\n"

LOG=/tmp/$(basename "$PROJECT")-run.log
cd "$SOVEREIGN" || die "cannot cd to $SOVEREIGN"
SOVEREIGN_PROFILE="$PROFILE" nohup caffeinate -ims ./supervisor.sh "$PROJECT" \
    --features-only > "$LOG" 2>&1 &
SUP=$!
echo -e "${DIM}  launched (pid $SUP), watching for ${WAIT}s...${RESET}\n"

for _ in $(seq "$WAIT"); do
    sleep 1
    kill -0 "$SUP" 2>/dev/null || break
done

sed 's/^/  /' "$LOG"
echo

if kill -0 "$SUP" 2>/dev/null; then
    echo -e "${GREEN}  ✓ running. Detaching — it keeps going when you close this.${RESET}"
else
    echo -e "${RED}  ✗ it exited already. The reason is above.${RESET}"
fi
echo -e "${DIM}  follow:  tail -f $PROJECT/logs/\$(date +%Y-%m-%d)-work.log${RESET}"
echo -e "${DIM}  console: tail -f $LOG${RESET}"
echo -e "${DIM}  stop:    pkill -9 -f supervisor.sh; pkill -9 -f work.py${RESET}"
