"""
Generates a multi-week roadmap from VISION.md and writes it to ROADMAP.md.
Reads existing ROADMAP.md to understand what's already been planned/completed.

Usage:
    python plan_week.py --project /path/to/project
    python plan_week.py --project /path/to/project --weeks 2 --tasks-per-day 22 --weekends
    python plan_week.py --project /path/to/project --start 2026-05-06
"""
import os
import sys
import argparse
import json
from datetime import date, timedelta
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


def get_week_dates(start: date, include_weekends: bool) -> list[date]:
    """Return days for a week starting from start date."""
    n = 7 if include_weekends else 5
    return [start + timedelta(days=i) for i in range(n)]


def parse_existing_roadmap(path: str) -> str:
    if os.path.exists(path):
        return open(path).read()
    return ""


def plan_one_week(
    week_start: date,
    vision: str,
    existing_roadmap: str,
    tasks_per_day: int,
    include_weekends: bool,
) -> list[dict]:
    """Call the model to plan one week. Returns list of day dicts."""
    days = get_week_dates(week_start, include_weekends)
    day_names = [d.strftime("%A") for d in days]
    day_list  = ", ".join(day_names)
    n_days    = len(days)

    existing_context = (
        f"\n\nAlready planned/completed (do not repeat these tasks):\n{existing_roadmap}"
        if existing_roadmap.strip() else ""
    )

    prompt = f"""You are planning a software development sprint starting {week_start.isoformat()}.

Project vision:
{vision}
{existing_context}

Generate a {n_days}-day plan ({day_list}) with exactly {tasks_per_day} surgical, micro-tasks per day.

STRICT TASK SIZING & DECOMPOSITION RULES:
1. SURGICAL SCOPE: Each task must touch 1 or 2 files MAXIMUM. If a feature needs 5 files, split it into 3 tasks.
2. NO AGGREGATE TASKS: Forbidden: "Integrate X with Y", "Implement X system", "Wire up Z".
3. MICRO-TASKS ONLY: Allowed: "Add [method] to [class] in [file]", "Define [interface] in [file]", "Update [handler] to check [condition]".
4. ATOMICITY: Each task must be a single, complete logical step.
5. NAMING: Tasks must name specific classes, methods, or files.
6. DONE-WHEN CLAUSE: Every task MUST end with a "— done when:" clause describing the observable result.
   Example: "Add update(dt) to VectorComponent that moves position toward _target at speed px/s — done when: units visibly travel across the screen toward a tapped star."
   Example: "In GestureHandler.onDragUpdate, draw a selection rect overlay on the canvas — done when: dragging on the game screen shows a neon rectangle following the finger."
   Tasks without a done-when clause are rejected.
7. NO STUBS: Tasks must result in working code, not placeholder method bodies. If the task says "implement X", the method body must contain real logic, not comments.
6. COMPATIBILITY: Each task must be small enough for a 3B-active model to finish in 120 seconds.

Example of good decomposition for "Add sync circuit breaker":
- Task A: "Define CircuitBreaker class in lib/game/circuit_breaker.dart — done when: class exists with isClosed() returning bool."
- Task B: "Add isClosed() check to GcpSyncHandler.sync() in lib/game/gcp_sync_handler.dart — done when: sync() returns early if circuit is open."
- Task C: "Implement recordFailure() in CircuitBreaker and call from GcpSyncHandler catch block — done when: three consecutive failures open the circuit."

Example of good gameplay task:
- "In GestureHandler.onDragUpdate in lib/game/gesture_handler.dart, accumulate drag delta into a SelectionRect and paint a neon cyan rectangle on the canvas — done when: dragging a finger on the game screen shows a glowing rectangle."

Return ONLY valid JSON with this exact structure (no markdown, no explanation):
{{
  "days": [
    {{
      "date": "{days[0].isoformat()}",
      "day": "{day_names[0]}",
      "tasks": ["task 1 — done when: observable result", "task 2 — done when: observable result", ... {tasks_per_day} tasks total]
    }},
    ... one entry per day, {n_days} total
  ]
}}"""

    raw = ollama(
        system="You are a senior software engineer planning a development sprint. Return only valid JSON. Every task must touch 1-2 files maximum, be completable in a single coding pass, and end with a 'done when:' clause describing the observable result.",
        user=prompt,
        timeout=600,
    )

    start_idx = raw.find("{")
    end_idx   = raw.rfind("}") + 1
    try:
        plan = json.loads(raw[start_idx:end_idx])
        return plan.get("days", [])
    except Exception:
        print(f"  ⚠  Could not parse JSON for week of {week_start}. Raw:\n{raw[:500]}")
        return []


def main():
    parser = argparse.ArgumentParser(description="Generate a multi-week ROADMAP.md from VISION.md")
    parser.add_argument("--project",       help="Path to the project folder (default: current dir)", default=None)
    parser.add_argument("--start",         help="Start date (YYYY-MM-DD, default: today)", default=None)
    parser.add_argument("--weeks",         help="Number of weeks to plan (default: 1)", type=int, default=1)
    parser.add_argument("--tasks-per-day", help="Tasks per day (default: 3)", type=int, default=3)
    parser.add_argument("--weekends",      help="Include Saturday and Sunday", action="store_true")
    parser.add_argument("--append",        help="Append to existing ROADMAP.md instead of overwriting", action="store_true")
    args = parser.parse_args()

    if args.project:
        project_path = os.path.abspath(args.project)
        if not os.path.isdir(project_path):
            print(f"⚠  Project folder not found: {project_path}")
            sys.exit(1)
        os.chdir(project_path)
        print(f"Project: {project_path}")

    today = date.today()
    start = date.fromisoformat(args.start) if args.start else today

    vision = open("VISION.md").read() if os.path.exists("VISION.md") else "No VISION.md found."

    total_days  = (7 if args.weekends else 5) * args.weeks
    total_tasks = total_days * args.tasks_per_day
    print(f"Planning {args.weeks} week(s) × {7 if args.weekends else 5} days × {args.tasks_per_day} tasks = {total_tasks} tasks total")
    if args.weekends:
        print("  (including weekends)")

    all_tasks = []
    rolling_roadmap = parse_existing_roadmap("ROADMAP.md")

    for week_num in range(args.weeks):
        week_start = start + timedelta(weeks=week_num)
        print(f"\nPlanning week {week_num + 1}/{args.weeks} starting {week_start.isoformat()}...")

        days = plan_one_week(
            week_start=week_start,
            vision=vision,
            existing_roadmap=rolling_roadmap,
            tasks_per_day=args.tasks_per_day,
            include_weekends=args.weekends,
        )

        if not days:
            print(f"  ⚠  No tasks generated for week {week_num + 1}, skipping.")
            continue

        for day in days:
            tasks = day.get("tasks", [])
            if len(tasks) < args.tasks_per_day:
                print(f"  ⚠  {day['day']}: got {len(tasks)} tasks, expected {args.tasks_per_day}")
            all_tasks.extend(tasks)

        # Feed this week's tasks back as context for the next
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
    print("Run work.py to start. standup.py reports velocity each morning.")


if __name__ == "__main__":
    main()
