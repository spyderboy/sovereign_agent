# The Xanadu console

A local web page for running Xanadu without editing files: pick the models,
choose where each one runs (this machine, a RunPod GPU, or another server),
set the limits for the next run, launch it, watch it, stop it.

It runs on this machine only. Nothing on the network can reach it.

## First-time setup on a new machine

1. **Python 3.11 or newer** and **git**.
2. **Ollama** from ollama.com, running (`ollama serve`, or the Ollama app).
3. In the Xanadu folder:

   ```bash
   cd sovereign_agent
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-core.txt
   ```

   (`requirements.txt` adds optional features — RAG retrieval, Firestore sync,
   Claude escalation — and is much larger. You don't need it to run.)
4. Pull the models you plan to run locally, e.g.
   `ollama pull qwen2.5-coder:14b` and `ollama pull qwen3.8:27b`.
5. Start the console:

   ```bash
   ./console.sh
   ```

   It opens http://127.0.0.1:8765. Set **Projects folder** at the top to the
   folder that holds your projects (each project needs a `ROADMAP.md` and a
   `.sovereign_config.json`).

## A run, in five steps

1. **Pick the project** on the left.
2. **Plan tab** — set the fast pass and the strong pass (below), then **Save plan**.
3. **Run tab** — **Launch run**. Launch stops anything left over, returns the
   project to a clean state, and starts the run. It refuses if the project has
   uncommitted changes: commit or discard those first, because Xanadu never
   treats a person's edits as scrap.
4. **Watch** — the Run tab refreshes every 10 seconds: progress, the current
   pass, alerts, recent events. **Logs** has the detail.
5. **Stop run** when you want to (it asks you to confirm). A run also stops on
   its own when a whole cycle makes no progress.

## Choosing models

A run alternates two passes. The **fast pass** clears easy tasks quickly; the
**strong pass** takes what the fast pass couldn't.

| Machine | Fast pass | Strong pass |
|---|---|---|
| Mac, 32 GB | `qwen2.5-coder:14b`, 2 workers | `qwen3.8:27b`, 1 worker |
| Mac, 64 GB+ | `qwen2.5-coder:14b` or `:32b` | a 30–35B model |
| Any machine + RunPod | a local small model | a 70B-class model on RunPod |

- **Size** should match the model (the console fills it in for suggested
  models). It decides which pass may run where.
- **Workers above 1** only make sense for a small model on this machine.
- **Thinking**: reasoning models (qwen3.x, deepseek-r1) think before answering.
  A benchmark on 2026-09-28 found thinking is what makes them right (4 of 4
  vs 2 of 4 with it off), at about twice the time. Leave it on "Model decides"
  unless a benchmark on your project says otherwise.
- **Per-attempt limit** stops one attempt from running forever. 15 minutes
  suits a 27B model on a Mac.
- **Check installed models** confirms the model is really there before you launch.

## Running a pass on RunPod

1. **RunPod tab** — paste an API key from runpod.io → Settings → API Keys.
   It is stored in this folder's `.env`, readable only by your user.
2. **Plan tab** — set a pass's **Runs on** to **RunPod**, then in **RunPod for
   this run** choose the GPU and a **spending cap**. A 70B model at 4-bit
   needs a 48 GB GPU (RTX A6000, L40S) or an 80 GB one.
3. Launch as usual.

What happens: before that pass, Xanadu starts (or creates) a pod named
`xanadu-<project>-<pass>`, waits for it, and downloads the model onto the
pod's storage — once; later runs reuse it. Before the other pass, it stops the
pod so you don't pay for it idling. A guard stops the pods when the run ends,
or as soon as the spending cap is reached.

- **Stop** keeps the downloaded models (a small storage charge per day).
  **Delete** removes the pod and its storage entirely.
- The console only ever sees or touches pods named `xanadu-…` — never other
  pods on the account.
- Your code reaches the pod over HTTPS through RunPod's proxy. If your
  policy doesn't allow code on third-party machines at all, use **Another
  server** with a GPU machine you control instead.

## Running a pass on another server

Any machine running Ollama that this one can reach — a GPU server in your
office or your own cloud account. Set **Runs on** to **Another server** and
enter its address, e.g. `http://gpu-box.internal:11434`. Use **Check this
server's models** to confirm the connection and the model.

## When the console shows an alert

| Alert | What it means | What to do |
|---|---|---|
| same error 3 attempts running | Something constant is wrong, not the model | Check the task's test and wording; a bigger model won't help |
| skipped N tasks in a row, same error | One cause is blocking many tasks | Read the error; usually a missing or misnamed part of the design |
| workers are coding without their tests | The worker can't see the project's files | Stop the run; this is a setup problem |
| one attempt running 25 min | A model that won't stop | Set a per-attempt limit for that pass |
| run stopped unexpectedly | It crashed | If auto-restart is on it restarts itself; otherwise Launch again |
| run parked for a human | A task needs a decision | Read `logs/escalate*.md` in the project |
| RunPod budget reached | The cap was hit; pods were stopped | Raise the cap or switch the pass to local |

## Rules that keep work safe

- **Don't edit the project while a run is live.** Workers reset the files
  between tasks. Stop the run, edit, commit, then launch.
- **Commit your changes before launching.** Launch refuses otherwise.
- **Measure before changing models or settings for good:**
  `python3 bench.py run --suite <suite> --profile <profile> --sample 6` —
  see `NEW_PROJECT.md`.

## Starting a brand-new project

See `NEW_PROJECT.md`. The one step not to skip: run
`python3 kickoff.py <project> --apply --commit` before the first launch.

## For the command line

Everything the console does is also available as:

```bash
python3 xrun.py show   <project>    # the saved plan, with defaults
python3 xrun.py launch <project>
python3 xrun.py status <project>
python3 xrun.py stop   <project>
```

Plans are saved in `runs/<project>.json`; the console writes the matching
profiles to `profiles/run-<project>-{cheap,strong}.toml` at launch.
