# Starting a new project with Xanadu

The command sequence, in order. The authoring process that comes before it
(spec, locked contract, tests, task discipline) is in the `xanadu-new-project`
skill and `docs/TASK_DISCIPLINE.md`.

## 1. Scaffold

```bash
python3 init_project.py ~/Code/<project> --name "<Name>"
```

Then **rewrite `.roorules` and `VISION.md`** — both are injected into every
worker prompt, and the template's placeholders describe a different app.

## 2. Author the kit

`spec/`, the locked contract, a test per behavioural file, `ROADMAP.md` (one
obligation per task, each ending `task gate: <cmd> passes`), and
`.sovereign_config.json` (`language`, `validate_commands`, `locked_files`,
`locked_prefixes`, `context_always_include`).

## 3. Check the kit

```bash
python3 check_task_size.py ~/Code/<project>      # one obligation per task
python3 api_surface.py ~/Code/<project> --check  # no locked type named without its shape
python3 preflight.py ~/Code/<project>             # broken done tasks, coverage, order, size
python3 preflight.py ~/Code/<project> --solve qwen38   # dry-run every open task (Ollama free)
python3 <project>/tools/preflight.py             # if the project has one
```

Every gate must be satisfiable and every analyze-style gate must pass on the
clean tree before the first run.

## 4. Kickoff — before the first sweep

```bash
python3 kickoff.py ~/Code/<project>                   # report
python3 kickoff.py ~/Code/<project> --apply --commit  # write + commit
```

A new project otherwise pays for every mistake the first time (2026-09-28:
xmeasure's store layer failed three passes on invented names while the fast
pass kept retrying tasks it had already failed). Kickoff puts in place up
front what a mature project accumulates:

- **seeds guards** from earlier projects in the same language that transfer
  (no foreign type names; none that match this project's committed code);
- **generates `docs/API_ROWS.md`** from the locked contract — fields, enum
  members, table columns — and adds it to `context_always_include`;
- **flags tasks** that name a locked type without showing its shape;
- **adds a routing policy** (`sov/routing.py`): each new layer's first tasks,
  and any task small models have failed twice, go to the strong pass instead
  of the fast one. A layer's first merges become the examples later tasks
  copy, so they should be good ones.

## 5. Run

```bash
MAIN_BRANCH=main CHEAP=qwen25-t1 STRONG=qwen38 CHEAP_WORKERS=2 STRONG_WORKERS=1 \
  nohup ./sweep.sh ~/Code/<project> > ~/Code/<project>/logs/sweep-console.log 2>&1 &
nohup .venv/bin/python -u monitor.py ~/Code/<project> --summary-every 60 \
  --restart "MAIN_BRANCH=main CHEAP=qwen25-t1 STRONG=qwen38 CHEAP_WORKERS=2 STRONG_WORKERS=1" \
  > ~/Code/<project>/logs/monitor.log 2>&1 &
```

Never edit the project repo while a run is live, and never edit a running
shell script in place. The `xanadu-run-control` skill has the full stop /
clean / relaunch sequence.

## 6. After every run — keep harvesting

```bash
python3 api_surface.py ~/Code/<project> --mine           # review
python3 api_surface.py ~/Code/<project> --mine --apply   # then commit the config
```

Each observed wrong guess becomes a guard, so it costs one attempt once,
project-wide. Works for Dart analyzer errors and Python runtime errors
(`AttributeError`, `ImportError`, unexpected constructor keywords, unknown
table columns).

## Measuring changes

```bash
python3 bench.py make --project ~/Code/<project> --name <project>-v1
python3 bench.py run --suite <project>-v1 --profile qwen38 --sample 6 --label before
python3 bench.py compare bench/results/<A> bench/results/<B>
```

Benchmark any harness, profile or model change before trusting it on a live run.
