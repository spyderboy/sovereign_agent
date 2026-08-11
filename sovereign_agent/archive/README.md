# Archive — dead execution paths

Archived 2026-07-10. These files belong to two separate, non-operational predecessor
architectures. Neither is used by the live pipeline (`supervisor.sh` → `work.py`,
or the RunPod scripts in `../runpod/`). Full analysis: see
`../../sovereign_agent_architecture_audit.md`.

## GCP Pub/Sub + Firestore + Cloud Run path
`orchestrate.py`, `worker.py`, `gcp/` (Pub/Sub setup, Cloud Run job spec, VM start
script, worker entrypoint), `run_local.sh` (despite the name, just invokes `worker.py`
and inherits the same GCP/Firestore hard requirement).

Confirmed non-operational: `orchestrate.py`'s own docstring says "two-tier pipeline"
and only defines Pub/Sub topics for tiers 1–2, while `worker.py` was upgraded to the
current 4-tier model set — tiers 3–4 are unreachable from the orchestrator. The
project also never got a GitHub remote (`PROJECT_REPO` unset), which cloud workers
need to pull source. Never exercised in the current Galaxican work.

## LangGraph + Dapr predecessor
`app.py`, `graph.py`, `nodes.py`, `state.py`, `validate.py`, `Dockerfile`,
`dapr-init.sh`, `DAPR_README.md`. An earlier architect/executor/validator/publisher
graph built on LangGraph with Dapr-backed state, designed around a manual "Roo"
(VSCode extension) workflow — `standup.py` still prints a leftover reference to
`python validate.py` in its `.roo-mission.md` output, which is the one remaining
loose end from this era (harmless — it's a print statement, not a call site).

This is also the likely source of the "4B model" references that don't match
anything in `.env` — `nodes.py`'s `VALIDATOR_MODEL` defaulted to `qwen3:4b`. Later
docs (`OVERVIEW.md`, `qwen_advisor.py` docstring) appear to have inherited that
detail by copy-paste rather than by describing the current `work.py` config.

## Restoring something from here
Everything is intact, just moved. `git mv` it back (or copy back) if you ever need
to resurrect the cloud path — the GCP prerequisites listed in
`../CLAUDE.md`'s "Cloud Run Startup Sequence" section would still need to be
completed first (GitHub remote, Docker image, Pub/Sub topics, VM).
