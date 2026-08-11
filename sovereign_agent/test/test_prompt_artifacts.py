"""
Regression tests for prompt_artifacts.verify_prompt_artifact.

The cases below are the real ones from the 2026-07-14 incident, where four
model-drafted rules were promoted into GalaxicanGo's .roorules:
  - one instructing models to call `.IsNeutral()` (method does not exist)
  - one referencing FindClosestStarToPos (does not exist)
  - two Dart/Flutter rules that leaked in from the astro-flux project

Run:  python -m pytest test/test_prompt_artifacts.py -q
      python test/test_prompt_artifacts.py          (no pytest needed)
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompt_artifacts as pa  # noqa: E402


# ── Fixture: a synthetic Go project with a realistic identifier surface ──────

GO_SOURCE = """
package game

import (
    "fmt"
    "time"
)

type Faction int

const (
    FactionNeutral Faction = iota
    FactionRed
    FactionBlue
)

type Squad struct {
    ID       int
    Name     string
    Owner    Faction
    Position Vec
    MaxHP    int
    CurHP    int
}

type Vec struct{ X, Y float64 }

type Star struct {
    ID      int
    Owner   Faction
    Coord   Vec
    Garrison *Squad
}

type Galaxy struct {
    Stars  []*Star
    Squads []*Squad
    Turn   int
}

func NewGalaxy(seed int64) *Galaxy { return &Galaxy{Turn: 0} }

func (g *Galaxy) Step(dt time.Duration) error { return nil }
func (g *Galaxy) StarByID(id int) *Star       { return nil }
func (g *Galaxy) SquadByID(id int) *Squad     { return nil }
func (g *Galaxy) OwnedStars(f Faction) []*Star { return nil }
func (s *Squad) MoveTo(target Vec) error      { return nil }
func (s *Squad) Damage(amount int) bool       { return false }
func (s *Squad) IsAlive() bool                { return s.CurHP > 0 }
func (s *Star) Capture(f Faction) error       { return nil }
func (s *Star) Distance(other *Star) float64  { return 0 }
func (f Faction) String() string              { return fmt.Sprint(int(f)) }

type Renderer interface {
    DrawStar(s *Star)
    DrawSquad(sq *Squad)
    DrawOverlay(text string)
}

type EventKind int

type Event struct {
    Kind      EventKind
    SourceID  int
    TargetID  int
    Timestamp time.Time
}

type EventBus struct{ Handlers []func(Event) }

func (b *EventBus) Publish(e Event)              {}
func (b *EventBus) Subscribe(h func(Event))      {}
func (b *EventBus) UnsubscribeAll()              {}

type CombatResolver struct{ Galaxy *Galaxy }

func (c *CombatResolver) Resolve(a, d *Squad) error { return nil }
func (c *CombatResolver) EstimateOutcome(a, d *Squad) float64 { return 0 }

type PathFinder struct{ Galaxy *Galaxy }

func (p *PathFinder) ShortestPath(from, to *Star) []*Star { return nil }
func (p *PathFinder) Reachable(from *Star, jumps int) []*Star { return nil }

type SaveGame struct {
    Version   int
    Galaxy    *Galaxy
    CreatedAt time.Time
}

func LoadSaveGame(path string) (*SaveGame, error) { return nil, nil }
func WriteSaveGame(sg *SaveGame, path string) error { return nil }

type TurnScheduler struct{ Pending []Event }

func (t *TurnScheduler) Enqueue(e Event) {}
func (t *TurnScheduler) Drain() []Event  { return nil }
func (t *TurnScheduler) HasWork() bool   { return false }
"""


def _filler_source(i: int) -> str:
    """Distinct identifiers so the fixture's UNIQUE token count clears
    MIN_WHITELIST_TOKENS — a real project has thousands; six copies of the
    same file have ~65, which would silently skip the grounding check."""
    lines = [f"package filler{i}", ""]
    for j in range(40):
        n = f"Sys{i}{j}"
        lines += [
            f"type {n}Config struct {{ Enabled{n} bool; Limit{n} int }}",
            f"func New{n}(c {n}Config) *{n}Config {{ return &c }}",
            f"func (c *{n}Config) Apply{n}() error {{ return nil }}",
        ]
    return "\n".join(lines) + "\n"


def make_go_project() -> str:
    """A temp Go project large enough to clear MIN_WHITELIST_TOKENS."""
    root = tempfile.mkdtemp(prefix="pa_go_")
    with open(os.path.join(root, ".sovereign_config.json"), "w") as f:
        f.write('{"language": "go", "project": "fixture"}')
    with open(os.path.join(root, "game.go"), "w") as f:
        f.write(GO_SOURCE)
    for i in range(4):
        with open(os.path.join(root, f"filler_{i}.go"), "w") as f:
            f.write(_filler_source(i))
    return root


def make_empty_project(language: str = "go") -> str:
    root = tempfile.mkdtemp(prefix="pa_empty_")
    with open(os.path.join(root, ".sovereign_config.json"), "w") as f:
        f.write('{"language": "%s"}' % language)
    return root


# ── The 2026-07-14 poisoned rules ────────────────────────────────────────────

POISONED = [
    # The one that cost ~30 blocked attempts.
    "Before capturing a star, check star.IsNeutral() to avoid redundant "
    "capture calls on stars you already own.",
    # Invented finder method.
    "Use g.FindClosestStarToPos(pos) rather than iterating Galaxy.Stars "
    "manually when locating a target.",
    # Dart/Flutter rules that leaked from astro-flux into the Go project.
    "Use Riverpod 2.x Notifier<T> with @override T build() and NotifierProvider; "
    "never use StateNotifier.",
    "Spawn the visual effect on parent before calling removeFromParent() and "
    "guard with a _dead flag.",
]

# Rules that are correct for this project and must NOT be rejected.
LEGITIMATE = [
    "When a squad reaches zero hit points, call Squad.Damage and check "
    "IsAlive before removing it from Galaxy.Squads.",
    "Always resolve combat through CombatResolver.Resolve — never mutate "
    "CurHP directly from the turn loop.",
    "Use PathFinder.ShortestPath instead of hand-rolling a search over Stars.",
    "Every exported function must return an error rather than panicking.",
    "Run gofmt before committing; go vet must pass with zero findings.",
]


# ── Tests ────────────────────────────────────────────────────────────────────

def test_poisoned_rules_are_rejected():
    root = make_go_project()
    for rule in POISONED:
        v = pa.verify_prompt_artifact(rule, root, kind="rule", mode="reject")
        assert not v.ok, f"should have been rejected: {rule[:70]}"
        assert v.reasons, "rejection must carry a reason"


def test_isneutral_specifically():
    """The exact identifier from the incident."""
    root = make_go_project()
    v = pa.verify_prompt_artifact(POISONED[0], root, kind="rule", mode="reject")
    assert not v.ok
    assert any("IsNeutral" in r for r in v.reasons), v.reasons


def test_dart_rules_flagged_as_foreign_on_go_project():
    root = make_go_project()
    v = pa.verify_prompt_artifact(POISONED[2], root, kind="rule", mode="reject")
    assert not v.ok
    assert any("dart" in r for r in v.reasons), v.reasons


def test_foreign_check_is_bidirectional():
    """The old hardcoded check only ran on Go projects. Go rules must be
    rejected on a Dart project too."""
    root = make_empty_project("dart")
    go_rule = "Run gofmt and go vet before every commit; err != nil must be handled."
    v = pa.verify_prompt_artifact(go_rule, root, kind="rule", mode="reject")
    assert not v.ok
    assert any("go" in r for r in v.reasons), v.reasons


def test_legitimate_rules_pass():
    root = make_go_project()
    for rule in LEGITIMATE:
        v = pa.verify_prompt_artifact(rule, root, kind="rule", mode="reject")
        assert v.ok, f"false positive on: {rule[:70]} → {v.summary()}"


def test_greenfield_project_does_not_reject_on_grounding():
    """No source to ground against → decline to judge, do not reject."""
    root = make_empty_project("go")
    v = pa.verify_prompt_artifact(
        "Use Galaxy.StarByID to look up a star.", root, mode="reject"
    )
    assert v.ok
    assert "grounding" in v.skipped


def test_warn_mode_never_rejects():
    root = make_go_project()
    v = pa.verify_prompt_artifact(POISONED[0], root, kind="rule", mode="warn")
    assert v.ok
    assert v.warnings


def test_existing_global_rules_produce_no_go_false_positives():
    """global_rules.md ships Riverpod/Flame content to every project. On a Go
    project the foreign-vocab check should catch that — it is the bug, not a
    false positive. But the universal sections must stay clean."""
    root = make_go_project()
    universal = (
        "A task is ONLY complete when every method body contains WORKING code. "
        "Make MINIMAL changes: touch only the files the task requires. "
        "Do NOT refactor or improve unrelated code in the same task. "
        "A typical task touches 1-3 files."
    )
    v = pa.verify_prompt_artifact(universal, root, mode="reject")
    assert v.ok, v.summary()


def test_prohibition_rules_are_not_flagged():
    """A rule forbidding an identifier must name it. Found against the real
    global_rules.md: 'never use StateNotifier (Riverpod 1.x — removed)' was
    rejected on a Dart project for referencing StateNotifier."""
    root = make_go_project()
    for rule in [
        "Never use Galaxy.LegacyStep — it was removed in the v2 refactor.",
        "Do not call squad.oldMoveTo(); use Squad.MoveTo instead.",
        "Use CombatResolver.Resolve rather than DirectDamageApply.",
    ]:
        v = pa.verify_prompt_artifact(rule, root, mode="reject")
        assert v.ok, f"false positive on prohibition rule: {v.summary()}"


def test_prohibition_does_not_whitelist_normal_use():
    """Mentioning an identifier once in a prohibition does not license using
    it as an instruction elsewhere in the same rule."""
    root = make_go_project()
    rule = ("Never use Galaxy.LegacyStep. Call Galaxy.PhantomStep every tick "
            "to advance the simulation.")
    v = pa.verify_prompt_artifact(rule, root, mode="reject")
    assert not v.ok
    assert any("PhantomStep" in r for r in v.reasons), v.reasons


def test_fenced_examples_are_skipped_by_default():
    """global_rules.md's anti-stub section illustrates forbidden code in a
    fence; onEvent there is deliberately fake."""
    root = make_go_project()
    rule = ("A task is only complete when every method body works.\n"
            "```\nFuture<void> onEvent(ImaginaryThing e) async { }\n```\n"
            "Escalate rather than writing a stub.")
    assert pa.verify_prompt_artifact(rule, root, mode="reject").ok
    strict = pa.verify_prompt_artifact(rule, root, mode="reject",
                                       check_code_blocks=True)
    assert not strict.ok, "check_code_blocks=True must still inspect fences"


def test_verifier_never_raises_on_garbage_input():
    for bad_root in ("/nonexistent/path/xyz", ""):
        v = pa.verify_prompt_artifact("Some rule about Foo.Bar", bad_root)
        assert isinstance(v, pa.Verdict)
    v = pa.verify_prompt_artifact("", make_go_project())
    assert v.ok


def test_partition_splits_batch():
    root = make_go_project()
    accepted, rejected = pa.partition(POISONED + LEGITIMATE, root, mode="reject")
    assert len(accepted) == len(LEGITIMATE), [a[:50] for a in accepted]
    assert len(rejected) == len(POISONED)


# ── Runner (no pytest required) ──────────────────────────────────────────────

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}\n      {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name} — ERROR {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
