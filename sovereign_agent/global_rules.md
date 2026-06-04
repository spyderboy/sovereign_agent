# Global Sovereign Agent Rules
# Applies to every project. Loaded before project-specific .roorules.

## !! ANTI-STUB RULE — READ FIRST, APPLY ALWAYS !!

A task is ONLY complete when every method body contains WORKING code.
The following patterns are FORBIDDEN in any language:

```
// FORBIDDEN — stub with comment body
void tick(double dt) {
  // Logic goes here.
}

// FORBIDDEN — empty loop body
for (var item in items) {
  // handle item
}

// FORBIDDEN — empty callback
@override
Future<void> onEvent(Event e) async {
  // TODO
}
```

If you cannot implement a method body with real logic, ESCALATE (exit 2).
Do NOT write a stub and mark the task done. Stubs break future tasks silently.

Every task must have an observable result: something a user sees, or something
a static analyser can verify. If there is no observable result, you have not
finished the task.

---

## Task execution rules

- Make MINIMAL changes: touch only the files the task requires.
- Do NOT refactor or improve unrelated code in the same task.
- Do NOT create new files unless the task explicitly says to create one.
- A typical task touches 1–3 files. If you find yourself changing more than 5, stop and reconsider.
- When a task says "remove X from file Y", verify X is actually present before changing anything.
  If it's already gone, mark done without touching the file.

---

## Locked-file workaround pattern

When a task requires modifying a locked file, implement the feature as a
dedicated system class + companion mixin placed alongside existing systems.
The mixin provides the wiring API so the locked file can receive a minimal
targeted patch in a later task, while the logic is immediately testable.

---

## Test tasks

When a test file imports a component that does not yet exist:
- Create the component implementation first with the exact constructor
  signature and public API the test expects.
- Never modify the test to work around a missing implementation.

When a task is labelled "test-only":
- Write ONLY to test/ files.
- If the feature being tested doesn't exist, write a skip-marked placeholder:
  `test('...', () {}, skip: 'feature not implemented');`
- Do NOT add the implementation yourself. Move on.

---

## Riverpod rules (any Flutter/Riverpod project)

- Use Riverpod 2.x `Notifier<T>` with `@override T build()` and `NotifierProvider`.
- Never use `StateNotifier` (Riverpod 1.x — removed).
- Providers that expose a slice of state must be `Provider<T>` that `ref.watch(...)` the root notifier.
- When a feature costs resources, read the resource provider first, guard with
  a length check, call the removal methods, then mutate local state.

---

## Death/destruction visual effects (any Flame project)

When implementing a destruction effect on a PositionComponent:
- Spawn the visual effect on `parent` BEFORE calling `removeFromParent()`.
- Guard with a `_dead` flag to prevent double-firing.
- The effect component must manage its own lifetime via `_elapsed >= _duration → removeFromParent()`.

---

## Learned Rules
# General orchestration lessons promoted from project runs.
# Rules specific to a language, framework, or project live in that project's .roorules.
