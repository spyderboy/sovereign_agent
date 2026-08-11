"""
Generates a backlog of tasks from VISION.md and writes it to ROADMAP.md.
Reads existing ROADMAP.md to understand what's already been planned/completed.

No "week" or "day" concept — tasks are generated as one flat batch (in chunks
of --batch-size per model call, to keep prompts a sane size) and written under
a single date header. work.py works through ROADMAP.md checkboxes in order
regardless of date, so the date header is for human readability only.

Usage:
    python plan_week.py --project /path/to/project
    python plan_week.py --project /path/to/project --tasks 20
    python plan_week.py --project /path/to/project --tasks 500 --batch-size 25   # big RunPod batch
"""
import os
import sys
import argparse
import json
import math
from datetime import date
from dotenv import load_dotenv
import requests

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

OLLAMA_URL    = os.getenv("LOCAL_MODEL_URL", "http://localhost:11434")
PLANNER_MODEL = os.getenv("PLANNER_MODEL",  "qwen2.5-coder:7b-instruct-q4_K_M")


def ollama(system: str, user: str, timeout: int = 600) -> str:
    import json as _json
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": PLANNER_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "stream": True,
        },
        stream=True,
        timeout=timeout,
    )
    resp.raise_for_status()
    content = []
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        try:
            chunk = _json.loads(raw_line)
        except _json.JSONDecodeError:
            continue
        delta = chunk.get("message", {}).get("content", "")
        if delta:
            content.append(delta)
        if chunk.get("done"):
            break
    return "".join(content)


def parse_existing_roadmap(path: str) -> str:
    if os.path.exists(path):
        return open(path).read()
    return ""


def plan_batch(
    n_tasks: int,
    vision: str,
    existing_roadmap: str,
) -> list[str]:
    """Call the model to generate one flat batch of n_tasks. Returns a list of task strings."""
    existing_context = (
        f"\n\nAlready planned/completed (do not repeat these tasks):\n{existing_roadmap}"
        if existing_roadmap.strip() else ""
    )

    prompt = f"""You are planning a software development backlog.

Project vision:
{vision}
{existing_context}

Generate exactly {n_tasks} surgical, micro-tasks.

STRICT TASK SIZING & DECOMPOSITION RULES — make every task the SMALLEST it can possibly be.
The 150-line file-size limit is a hard ceiling, not a sizing target. A task that produces
140 lines in one shot is still too big if it's really three unrelated changes bundled
together — smallness means ONE unit of work, not "whatever fits under the line limit."

1. ONE FILE: Each task touches exactly 1 file. Only touch a second file when the task is
   truly impossible without it (e.g. a new class plus the one call site that must reference
   it) — and even then, prefer splitting into two tasks if at all possible.
2. ONE UNIT OF WORK: One task = one added/changed method, one added class, one added widget,
   one fixed bug, one field. Never bundle multiple methods, multiple fields, or multiple
   fixes into a single task, even if they're all in the same file and even if the combined
   result would be well under 150 lines. If a task description contains "and" joining two
   separate actions ("add X and update Y"), split it into two tasks.
3. NO AGGREGATE TASKS: Forbidden: "Integrate X with Y", "Implement X system", "Wire up Z",
   "Add X, Y, and Z to [class]".
4. MICRO-TASKS ONLY: Allowed: "Add [method] to [class] in [file]", "Define [interface] in
   [file]", "Update [handler] to check [condition]".
5. ATOMICITY: Each task must be a single, complete logical step — the smallest step that is
   still independently meaningful and testable on its own.
6. NAMING: Tasks must name specific classes, methods, or files.
7. DONE-WHEN CLAUSE: Every task MUST end with a "— done when:" clause describing the observable result.
   Example: "Add update(dt) to VectorComponent that moves position toward _target at speed px/s — done when: units visibly travel across the screen toward a tapped star."
   Example: "In GestureHandler.onDragUpdate, draw a selection rect overlay on the canvas — done when: dragging on the game screen shows a neon rectangle following the finger."
   Tasks without a done-when clause are rejected.
8. NO STUBS: Tasks must result in working code, not placeholder method bodies. If the task says "implement X", the method body must contain real logic, not comments.
9. COMPATIBILITY: Each task must be small enough for a 7B model to finish in well under two minutes.

Example of good decomposition for "Add sync circuit breaker" — note this is FOUR tasks, not
one and not a bundled two:
- Task A: "Define CircuitBreaker class in lib/game/circuit_breaker.dart — done when: class exists with isClosed() returning bool."
- Task B: "Add recordFailure() to CircuitBreaker in lib/game/circuit_breaker.dart — done when: recordFailure() increments the internal failure count."
- Task C: "Add isClosed() check to GcpSyncHandler.sync() in lib/game/gcp_sync_handler.dart — done when: sync() returns early if circuit is open."
- Task D: "Call CircuitBreaker.recordFailure() from GcpSyncHandler's catch block in lib/game/gcp_sync_handler.dart — done when: three consecutive failures open the circuit."

Example of good gameplay task:
- "In GestureHandler.onDragUpdate in lib/game/gesture_handler.dart, accumulate drag delta into a SelectionRect and paint a neon cyan rectangle on the canvas — done when: dragging a finger on the game screen shows a glowing rectangle."

Return ONLY valid JSON with this exact structure (no markdown, no explanation):
{{
  "tasks": ["task 1 — done when: observable result", "task 2 — done when: observable result", ... {n_tasks} tasks total]
}}"""

    raw = ollama(
        system="You are a senior software engineer planning a development backlog. Return only valid JSON. Every task must touch exactly one file, be exactly one unit of work (one method, one class, one fix — never bundled), be completable in a single coding pass, and end with a 'done when:' clause describing the observable result.",
        user=prompt,
        timeout=600,
    )

    start_idx = raw.find("{")
    end_idx   = raw.rfind("}") + 1
    try:
        plan = json.loads(raw[start_idx:end_idx])
        return plan.get("tasks", [])
    except Exception:
        print(f"  ⚠  Could not parse JSON for this batch. Raw:\n{raw[:500]}")
        return []


def main():
    parser = argparse.ArgumentParser(description="Generate a task backlog from VISION.md into ROADMAP.md")
    parser.add_argument("--project",     help="Path to the project folder (default: current dir)", default=None)
    parser.add_argument("--start",       help="Date to label the backlog with (YYYY-MM-DD, default: today). Cosmetic only.", default=None)
    parser.add_argument("--tasks",       help="Total number of tasks to generate (default: 20)", type=int, default=20)
    parser.add_argument("--batch-size",  help="Max tasks requested per model call (default: 25)", type=int, default=25)
    parser.add_argument("--append",      help="Append to existing ROADMAP.md instead of overwriting", action="store_true")
    args = parser.parse_args()

    if args.project:
        project_path = os.path.abspath(args.project)
        if not os.path.isdir(project_path):
            print(f"⚠  Project folder not found: {project_path}")
            sys.exit(1)
        os.chdir(project_path)
        print(f"Project: {project_path}")

    start = date.fromisoformat(args.start) if args.start else date.today()

    vision = open("VISION.md").read() if os.path.exists("VISION.md") else "No VISION.md found."

    n_batches = math.ceil(args.tasks / args.batch_size)
    print(f"Planning {args.tasks} task(s) in {n_batches} batch(es) of up to {args.batch_size}...")

    all_tasks = []
    rolling_roadmap = parse_existing_roadmap("ROADMAP.md")
    remaining = args.tasks

    for batch_num in range(n_batches):
        batch_size = min(args.batch_size, remaining)
        print(f"\nBatch {batch_num + 1}/{n_batches} — requesting {batch_size} tasks...")

        tasks = plan_batch(
            n_tasks=batch_size,
            vision=vision,
            existing_roadmap=rolling_roadmap,
        )

        if not tasks:
            print(f"  ⚠  No tasks generated for batch {batch_num + 1}, skipping.")
            continue

        if len(tasks) < batch_size:
            print(f"  ⚠  Got {len(tasks)} tasks, expected {batch_size}")

        all_tasks.extend(tasks)
        remaining -= len(tasks)

        # Feed tasks generated so far back as context for the next batch.
        rolling_roadmap += "\n" + "\n".join(f"- [ ] {t}" for t in all_tasks[-100:])

    # All tasks go under a single date — work.py works through them in order.
    # Date headers are for human readability only, not execution gates.
    lines = [f"# Backlog — generated {start.isoformat()}\n",
             f"## {start.strftime('%A')} {start.isoformat()}"]
    for task in all_tasks:
        lines.append(f"- [ ] {task}")

    roadmap = "\n".join(lines) + "\n"

    if args.append and os.path.exists("ROADMAP.md"):
        existing = open("ROADMAP.md").read()
        with open("ROADMAP.md", "w") as f:
            f.write(existing.rstrip() + "\n\n" + roadmap)
        print("\n✓ Appended to ROADMAP.md")
    else:
        with open("ROADMAP.md", "w") as f:
            f.write(roadmap)
        print("\n✓ ROADMAP.md written")

    print(f"\nTotal tasks planned: {len(all_tasks)}")
    print("Run work.py to start. standup.py reports velocity each check-in.")


if __name__ == "__main__":
    main()
