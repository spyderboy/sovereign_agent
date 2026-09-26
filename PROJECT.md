---
status: active
stage: mvp
priority: high
hosting: none
repo: https://github.com/spyderboy/sovereign_agent.git
date_started: 2026-05-23T23:08:24-04:00
public_status: In daily use
---

# Xanadu

## Stack
- Python
- Anthropic API
- Google Cloud Firestore

## Marketing
### Tagline
An autonomous development loop: local LLMs plan, write, test and fix code
straight from a roadmap, escalating to Claude only when they're stuck.

### Promo Text
Xanadu is the engine behind everything else on this page. Give it a
project's ROADMAP.md and it works through the tasks unattended, one
validated change at a time, then surfaces only the blockers that genuinely
need a human. Most of the work runs on local models on hardware I already
own; paid cloud models are the last rung of the ladder, not the first.

## Features
- Works straight from a project's ROADMAP.md: one task, one tested change
- Tiered model ladder: four local LLM tiers first, Claude only after repeat failures
- Real validation gates: static analysis plus per-task tests on every attempt
- Learns from its mistakes: repeated error patterns become permanent rules shared across machines
- Scales from one Mac to parallel workers to rented RunPod GPUs
- Fully resumable: all state lives in the roadmap and git, so it survives being killed mid-run

## Notes
The tool behind most of the other roadmap-driven projects in this directory
(Galaxican, GalaxicanGo, GalaxicanJS, witches_bricks all carry
`.sovereign_config.json` files it consumes). See `OVERVIEW.md` for
`plan_week.py` / `standup.py` / `work.py`. Status/stage/priority are a first
pass — adjust in the dashboard if they don't match your read.
