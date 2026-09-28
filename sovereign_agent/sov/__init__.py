"""Sovereign agent internals, extracted from work.py.

work.py is the CLI and run orchestration (main() and its named steps). The
machinery lives here, layered so that imports only point downward:

  runner      run_task — the retry ladder: tiers, attempts, autofix, advisor
  escalation  escalate.md, Claude escalation, tier2 queue, incomplete report
  prompting   role specs per language, implement_task
  validation  baseline errors, validate commands, per-task gate, review parking
  files       source discovery, relevant files, reads, writes, locked registry
  guards      bad patterns, error hints, placeholder/stub detection
  deps        symbol index, not-yet-built import detection
  apistubs    read-only API stubs of locked files for prompts
  llm         Ollama client, model preflight, large-model lock, JSON extraction
  project     .sovereign_config.json loading (into state)
  lessons     Firestore sync of learned lessons
  telemetry   velocity, traces, race log, failure ledger
  budget      per-task time budgets, timing report
  scheduling  task chains and worker assignment for parallel runs
  gitops      task branches, merges, per-worker worktrees
  roadmap     ROADMAP.md parsing and done-marking
  watchdog    stall watchdog heartbeat
  lazytools   lazily imported autofix / import fixer / advisor
  state       values rebound during a run (read them as state.NAME)
  config      models, tiers, attempts, budgets (profiles + env)
  ui          terminal colours
"""
