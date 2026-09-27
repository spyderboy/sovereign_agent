---
status: active
stage: mvp
priority: high
hosting: none
repo: https://github.com/spyderboy/sovereign_agent.git
date_started: 2026-05-23T23:08:24-04:00
public_status: In daily use
cta_label: How it works
cta_url: '#engine'
---

# Xanadu

## Stack
- Python
- Anthropic API
- Google Cloud Firestore

## Marketing
### Tagline
The autonomous development loop behind the projects on this page.

### Promo Text
Give Xanadu a project's roadmap and it works through it unattended, one
tested change at a time, surfacing only the blockers that genuinely need a
human. The architecture and the numbers are in the Engine section.

## Features
- Works straight from a project's ROADMAP.md: one task, one tested change
- Runs unattended overnight, and picks up where it left off if it's stopped
- Comes back with a finished roadmap or a short list of decisions for a human

## Notes
The tool behind most of the other roadmap-driven projects in this directory
(Galaxican, GalaxicanGo, GalaxicanJS, witches_bricks all carry
`.sovereign_config.json` files it consumes). See `OVERVIEW.md` for
`plan_week.py` / `standup.py` / `work.py`. Status/stage/priority are a first
pass — adjust in the dashboard if they don't match your read.
