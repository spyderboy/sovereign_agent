# Task Discipline — writing tasks an LLM can actually finish

**Audience:** any human or LLM decomposing work into tasks for a coding agent —
**any project, any language, any framework.** Nothing here is stack-specific.
This is the distilled lesson from real autonomous runs: the model rarely fails
because it "can't code." It fails because the *task* let it guess. Fix the
task, not the model.

## The one-sentence rule

> **A task is right-sized when the worker cannot fail on design or integration —
> only on correctness — because every input, output, and dependency it needs is
> named explicitly and it has exactly one thing to do.**

If a task leaves room to *invent* an API, a field, a file path, or "how these
two pieces connect," it is too big or too vague — regardless of how few lines
the result will be. A file-length cap is a ceiling, not a sizing rule.

## The three things every task must name

1. **Input contract** — exact types/signatures the worker receives.
   *"accepts `(g: GameState, pos: Vec2)`, no other args."*
2. **Output contract** — exact type/shape it must produce.
   *"returns `string` (SVG path data), never null."*
3. **Definition of done** — a concrete gate, not a vibe.
   *"done when: `typecheck` passes and `p10_render.test.ts` runs without error"*
   — never *"done when: it renders nicely."*

## Contract fidelity — the failure that wastes the most attempts

The single most common loop: the worker references a member that **does not
exist** on a type/API it can't fully see, invents it, and fails over and over
(`Property 'X' does not exist`, `Cannot find name 'Y'`, `undefined_*`). This is
a *task-writing* failure. Prevent it:

- **List the real fields/signatures in the task itself.** If the task touches an
  interface, name its actual members. *"Star has {id,tier,owner,hp,pos} — no
  `selected`, no timer field."*
- **Name the exact import** (module path + symbol) for every dependency the task
  calls. Don't make the worker locate it.
- **Forbid the known-bad guesses by name.** If workers keep reaching for a wrong
  API, write the ban into the task: *"LassoDetector has ONLY
  onTapUp/onPointerDown/reset — no .center/.radius. Do NOT use addEventListener."*
- **Feed contracts through always-loaded context** (an `API.md`, the type file)
  so the worker sees them on every attempt, not just when it happens to open the
  right file.
- **For a PORT, give the worker the source it's porting.** "Port `foo.ts` to
  `foo.dart`" is unreliable if the worker can't see `foo.ts` — it will translate
  from your prose and invent APIs the target language "should" have. Put the
  original in front of it (mirror the source tree into a `reference/` dir the
  harness auto-includes for the referenced file). This turns *invent* into
  *translate* and is the single biggest reliability win on otherwise-trivial port
  tasks — a 4-line function should pass on attempt 1, not attempt 4.

## When to split a task

Split the moment any of these is true:

- The description contains **"and"** joining two units of work, or names more
  than one noun-of-work (*"render entities **and** wire gestures **and** run the
  loop"*).
- Completing it requires **inventing how two components connect** (framework ↔
  domain, UI ↔ state, network ↔ model). That glue is its own task.
- It **constructs several things** before doing its job (build state + build AI +
  build rng + seed + loop). Each construction is a candidate task.
- The signal from a run: **a worker fails 2+ times on "X doesn't exist" or on
  the same integration seam.** That's proof the task is wrong — rewrite/split it;
  do not just retry a bigger model.

## Isolate the hard part into a pure function

Integration logic (the glue that hallucinates most) belongs in a **pure function
with an exact signature**, separate from the framework shell. A pure
`handleTouchDown(g, lasso, pos, nowMs): void` is testable and un-guessable; the
same logic buried inside a UI component invites invented APIs and DOM/framework
confusion. Keep the shell thin: it only *composes* pieces that already exist.

## The gate must be able to FAIL a wrong answer

Decomposition and contracts make a task *answerable*; they do not make a wrong
answer *fail*. A done-gate that only proves "it compiles" will pass output that
is silently broken. Real example: a `drawStar` that returns SVG path strings
compiled cleanly while every string contained `2 * ${radius}` un-evaluated —
valid TypeScript, invalid SVG. The model got a green check for garbage and had
no signal to fix it.

- **Gate on behavior, not just types.** For a function that returns a string,
  data, or a computed value, add a check that *calls it and asserts the result
  is well-formed* (a regex, a parse, a known input→output case). Type-checking a
  function that returns `string` tells you nothing about the string.
- **Make the gate reject the specific failure you've seen.** Once you know the
  wrong shape (a literal operator in path data, a NaN, an empty array), assert
  against it by name.
- **Verify the gate actually RUNS and actually FAILS on known-bad input — or it
  is theater.** A gate the harness never executes is worse than none: it gives
  false confidence. Writing "should pass test X" as prose in a task does nothing
  if the runner only executes gates declared in a specific form. After adding a
  gate, prove it red on the current broken output before trusting it green.
  (Real miss: a behavioral test was written but phrased as a "done when" note the
  harness didn't parse, so the broken code was marked done anyway.)
- A task's done-gate is a promise: *if this passes, the work is correct.* If a
  wrong implementation can pass it, the gate — not the model — is the problem.
- **The gate must also be PASSABLE by a correct implementation.** The inverse
  failure is a gate no right answer can satisfy — usually a task that requires an
  API the toolchain doesn't provide (a runtime global not in the TS `lib`, a
  dependency not installed). The worker then loops forever on `Cannot find name`
  through no fault of its own. Before shipping a task, confirm a *known-good*
  implementation goes green under its gate; if it can't, fix the environment
  (declare the global, install the dep) — don't make the worker paper over it.

## Model priors override local truth — neutralize the conflict

An LLM carries strong priors from training (web `addEventListener`, default
exports, common field names like `selected`). When the repo's local truth
differs, the model reverts to the prior — especially deep in a long prompt.
Smaller models revert more; much of what parameter count buys is holding the
instruction against the prior. You cannot out-scale this reliably, so remove the
conflict:

- **Keep conventions consistent across the codebase.** One file that breaks the
  house style (a lone `export default` among named exports) becomes a landmine:
  every consumer now has to *guess* which style applies. Fix the outlier, don't
  document the inconsistency.
- **State the convention in always-loaded context and forbid the prior by name**
  (*"all modules use named exports; a default import fails with TS2613"*).
- **Order dependencies so a task never runs before what it imports exists.** A
  composition task run early fails on `Cannot find name` through no fault of its
  own. If your harness can, have it *defer* a task whose named dependencies are
  absent rather than burn attempts.

## Worked example — one bloated task → four small ones

A single "GameCanvas" task bundled ~7 concerns (resolve level data, build state,
build AI, build rng, run the ~60 FPS loop, compose all renderers, wire
touch→lasso/select/orders). The worker hallucinated a lasso API and used web
`addEventListener` in a native app, and failed 8 attempts. Splitting it fixed it:

1. `gameInput.ts` — **pure** touch→sim functions; exact real lasso API named, bad
   guesses forbidden.
2. `setupGame.ts` — **pure** `createGameForLevel(...)`; all construction wiring in
   one named place, exact imports listed.
3. `useGameLoop.ts` — just the requestAnimationFrame loop; `setInterval`/DOM
   listeners explicitly banned.
4. `GameCanvas.tsx` — **thin shell** that only composes the three above plus the
   display components; exact child list, exact framework touch props, exact data
   import path.

Each is one file, one concern, exact signatures, a `typecheck` gate. The worker
can no longer fail on design — only on correctness.

## Checklist before you ship a task

- [ ] One file, one unit of work — no "and".
- [ ] Exact input + output signatures written in the task.
- [ ] Every dependency named by module path + symbol.
- [ ] Real fields/members listed; known-bad guesses explicitly forbidden.
- [ ] A concrete done-gate (a command/test), not a subjective description.
- [ ] Any integration glue pushed into a pure function with a fixed signature.
- [ ] Contracts the task relies on are in always-loaded context.
