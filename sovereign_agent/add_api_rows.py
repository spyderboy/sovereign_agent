"""Add the turn/player rows to a project's API cheat sheet.

    python3 add_api_rows.py ~/Code/<project>/lib/sim/API.md

Idempotent — safe to re-run, adds only the rows that are missing.

WHY
===

The READ THIS FIRST table in API.md covered tiles, units, HP and mutators, and
nothing about the single most frequently needed fact in the sim: which player is
to move, and what the two players are called.

Every model that hit the gap invented a path around it:

    g.capabilities?.owner ?? neutral      qwen2.5-coder:14b   spellWater
    activePlayerId                        qwen2.5-coder:7b    spellAir, spellWater
    currentPlayer                         qwen2.5-coder:7b    spellLightning
    g.players / getCurrentTurn()          qwen2.5-coder:7b    spellLightning
    Player.player1 / Player.playerOne     both                victory  (11 traces)
    Player.id / Player.neutral            both

None exist. `victory.dart` spent 13 attempts and 83 minutes on `Player.player1`
before hitting the hard ceiling — the enum is `enum Player { one, two }`.

The lesson generalises past this project: a context file is not read evenly. A
model reads the table and skims the prose. If a fact is needed on most tasks it
belongs in the table, in the same "call exactly this / NOT that" shape as
everything else — and the NOT column is what actually kills the invented
spelling. Adding `g.activePlayer` without adding `Player.one` just moved the
guess one step sideways.
"""

import sys

# (anchor row it follows, the row to add, a marker proving it is present)
ROWS = [
    (
        "| A new unit id | `g.takeNextId()` — this one IS a method | generating your own |",
        "| Whose turn is it? | `g.activePlayer` — a plain `Player` FIELD | "
        "`g.capabilities.owner`, `currentPlayer`, `activePlayerId`, `getCurrentTurn()` |",
        "Whose turn is it?",
    ),
    (
        "`g.capabilities.owner`, `currentPlayer`, `activePlayerId`, `getCurrentTurn()` |",
        "| The two players | `Player.one` and `Player.two` — those are the ONLY two "
        "values | `Player.player1`, `Player.playerOne`, `Player.p1`, `Player.neutral` |",
        "The two players",
    ),
    (
        "`Player.player1`, `Player.playerOne`, `Player.p1`, `Player.neutral` |",
        "| The other player | `opponent(p)` from `types.dart` | "
        "`Player.values[1 - p.index]`, negating a bool |",
        "The other player",
    ),
    (
        "`Player.values[1 - p.index]`, negating a bool |",
        "| The id for an action | `u.id` of a unit that already exists | "
        "`g.takeNextId()` — that MINTS A NEW id, only for units you are creating |",
        "The id for an action",
    ),
]


def patch(path: str) -> str:
    src = open(path).read()
    added = 0
    for anchor, row, marker in ROWS:
        if marker in src:
            continue
        if anchor not in src:
            return (f"FAILED at '{marker}': anchor row not found. The cheat "
                    f"sheet may have been edited by hand — add the row manually.")
        src = src.replace(anchor, anchor + "\n" + row, 1)
        added += 1
    open(path, "w").write(src)
    return f"added {added} row(s)" if added else "already installed"


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "lib/sim/API.md"
    print(patch(target), "->", target)
