"""
Morning standup — run this at the start of each day.

Shows today's and tomorrow's planned tasks, lets you approve or pivot,
then kicks off the first task by writing .roo-mission.md.

Usage:
    python standup.py
    python standup.py --project /path/to/project
"""
import os
import re
import sys
import json
import argparse
import importlib.util
from datetime import date, timedelta
from dotenv import load_dotenv
import requests

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ─── Velocity import (sibling script, optional) ───────────────────────────────

def _load_velocity():
    """Load load_records + report from velocity.py sitting next to this script."""
    try:
        spec = importlib.util.spec_from_file_location(
            "velocity",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "velocity.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.load_records, mod.report
    except Exception:
        return None, None

OLLAMA_URL    = os.getenv("LOCAL_MODEL_URL", "http://localhost:11434")
PLANNER_MODEL = os.getenv("PLANNER_MODEL",  "qwen3:4b")  # pivot + task reformatting — light load

ROADMAP_PATH   = "ROADMAP.md"
APPROVED_PATH  = "today_approved.md"
LOG_DIR        = "logs"

# Target size for a planned day.  Standup will offer to top-up if tomorrow
# has fewer than this many open tasks.
MIN_DAY_TASKS = 22


# ─── Ollama helper ────────────────────────────────────────────────────────────

def ollama(system: str, user: str, model: str = None) -> str:
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model or PLANNER_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "stream": False,
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


# ─── ROADMAP parser ───────────────────────────────────────────────────────────

def parse_roadmap(path: str) -> dict[str, list[str]]:
    """
    Returns a dict of date_str → [task, task, ...].
    Tasks are the raw checkbox lines, with [ ] or [x] preserved.
    """
    if not os.path.exists(path):
        return {}

    days: dict[str, list[str]] = {}
    current_date = None
    date_pattern = re.compile(r"#{2,3}\s+\w+\s+(\d{4}-\d{2}-\d{2})")
    task_pattern = re.compile(r"^- \[.\] .+")

    for line in open(path):
        m = date_pattern.match(line.strip())
        if m:
            current_date = m.group(1)
            days[current_date] = []
        elif current_date and task_pattern.match(line.strip()):
            days[current_date].append(line.strip())

    return days


def get_open_tasks(tasks: list[str]) -> list[str]:
    """Return only unchecked tasks."""
    return [t for t in tasks if t.startswith("- [ ]")]


def task_text(task_line: str) -> str:
    """Strip the checkbox prefix, return the task description."""
    return re.sub(r"^- \[.\] ", "", task_line).strip()


def update_roadmap_day(path: str, target_date: str, new_tasks: list[str]):
    """Replace the tasks for a specific date in ROADMAP.md."""
    lines = open(path).readlines()
    output = []
    in_target = False
    date_pattern = re.compile(r"#{2,3}\s+\w+\s+(\d{4}-\d{2}-\d{2})")

    for line in lines:
        m = date_pattern.match(line.strip())
        if m:
            if m.group(1) == target_date:
                in_target = True
                output.append(line)
                for t in new_tasks:
                    output.append(f"- [ ] {t}\n")
                continue
            else:
                in_target = False
        if in_target and re.match(r"^- \[.\] ", line.strip()):
            continue  # skip old tasks for this day
        output.append(line)

    with open(path, "w") as f:
        f.writelines(output)


def _remove_tasks_from_section(path: str, target_date: str, task_lines: set[str]):
    """Remove specific raw task lines from a date section, preserving done tasks."""
    lines = open(path).readlines()
    output = []
    date_pattern = re.compile(r"#{2,3}\s+\w+\s+(\d{4}-\d{2}-\d{2})")
    in_target = False
    for line in lines:
        m = date_pattern.match(line.strip())
        if m:
            in_target = m.group(1) == target_date
        if in_target and line.strip() in task_lines:
            continue  # drop this task
        output.append(line)
    with open(path, "w") as f:
        f.writelines(output)


def _append_tasks_to_section(path: str, target_date: str, task_texts: list[str]):
    """Append new unchecked tasks after the last existing task in a date section.
    Creates the section header if it doesn't exist yet."""
    lines = open(path).readlines()
    date_pattern = re.compile(r"#{2,3}\s+\w+\s+(\d{4}-\d{2}-\d{2})")

    # Find insertion point: after the last task line in target_date's section
    in_target = False
    last_task_idx = -1
    next_section_idx = len(lines)
    target_header_idx = -1

    for i, line in enumerate(lines):
        m = date_pattern.match(line.strip())
        if m:
            if m.group(1) == target_date:
                in_target = True
                target_header_idx = i
            elif in_target:
                next_section_idx = i
                break
        elif in_target and re.match(r"^- \[.\] ", line.strip()):
            last_task_idx = i

    if target_header_idx == -1:
        # Section doesn't exist — append at end of file
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n### {target_date}\n")
        for t in task_texts:
            lines.append(f"- [ ] {t}\n")
        with open(path, "w") as f:
            f.writelines(lines)
        return

    insert_at = (last_task_idx + 1) if last_task_idx >= 0 else next_section_idx
    new_lines = lines[:insert_at]
    for t in task_texts:
        new_lines.append(f"- [ ] {t}\n")
    new_lines.extend(lines[insert_at:])
    with open(path, "w") as f:
        f.writelines(new_lines)


def fill_tomorrow_from_backlog(path: str, roadmap: dict[str, list[str]],
                                tomorrow_str: str, needed: int) -> list[str]:
    """Move `needed` unchecked tasks from future backlog dates into tomorrow.

    Tasks are physically moved (removed from source section, added to tomorrow).
    Returns the list of moved task texts for display.
    """
    to_move: list[tuple[str, str]] = []  # (source_date, raw_task_line)

    for d in sorted(roadmap.keys()):
        if d <= tomorrow_str:
            continue
        for t in roadmap[d]:
            if t.startswith("- [ ]"):
                to_move.append((d, t))
        if len(to_move) >= needed:
            break

    to_move = to_move[:needed]
    if not to_move:
        return []

    # Group by source date so we only rewrite each section once
    by_source: dict[str, list[str]] = {}
    for src_date, task_line in to_move:
        by_source.setdefault(src_date, []).append(task_line)

    for src_date, moved_lines in by_source.items():
        _remove_tasks_from_section(path, src_date, set(moved_lines))

    # Append to tomorrow (plain text, no checkbox prefix — _append adds it)
    texts = [task_text(t) for _, t in to_move]
    _append_tasks_to_section(path, tomorrow_str, texts)

    return texts


# ─── Display helpers ──────────────────────────────────────────────────────────

BOLD  = "\033[1m"
BLUE  = "\033[94m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RESET = "\033[0m"
DIM   = "\033[2m"


def print_header(today: date):
    print(f"\n{BOLD}{'━'*52}{RESET}")
    print(f"{BOLD}  🌅  Morning Standup — {today.strftime('%A, %B %d %Y')}{RESET}")
    print(f"{BOLD}{'━'*52}{RESET}\n")


def print_tasks(label: str, date_str: str, tasks: list[str], color=BLUE,
                warn_if_under: int = 0):
    open_tasks = get_open_tasks(tasks)
    done_tasks = [t for t in tasks if t.startswith("- [x]")]
    count_note = f"  {DIM}({len(open_tasks)} open){RESET}" if open_tasks else ""
    print(f"{color}{BOLD}{label}{RESET}  {DIM}({date_str}){RESET}{count_note}")
    if not tasks:
        print(f"  {DIM}No tasks planned.{RESET}")
    else:
        for i, t in enumerate(open_tasks, 1):
            print(f"  {i}. {task_text(t)}")
        for t in done_tasks:
            print(f"  {DIM}✓ {task_text(t)}{RESET}")
    if warn_if_under and len(open_tasks) < warn_if_under:
        shortfall = warn_if_under - len(open_tasks)
        print(f"  {YELLOW}⚠  Only {len(open_tasks)} tasks — {shortfall} below target ({warn_if_under}).  "
              f"Use [f] to pull {shortfall} more from backlog.{RESET}")
    print()


def print_menu():
    print(f"{BOLD}What would you like to do?{RESET}")
    print(f"  {GREEN}[y]{RESET}  Approve today's tasks and start work")
    print(f"  {YELLOW}[f]{RESET}  Fill tomorrow to {MIN_DAY_TASKS} tasks from backlog")
    print(f"  {YELLOW}[p]{RESET}  Pivot tomorrow's tasks (AI replan)")
    print(f"  {YELLOW}[e]{RESET}  Edit today's tasks")
    print(f"  [q]  Quit without starting")
    print()


# ─── Pivot ───────────────────────────────────────────────────────────────────

def pivot_tomorrow(tomorrow_str: str, current_tasks: list[str]):
    print(f"\n{YELLOW}{BOLD}Pivot tomorrow ({tomorrow_str}){RESET}")
    print("Current tasks:")
    for i, t in enumerate(get_open_tasks(current_tasks), 1):
        print(f"  {i}. {task_text(t)}")
    print()
    print("Describe the new direction for tomorrow")
    print(f"{DIM}(e.g. 'focus on deployment instead of auth' or 'add these specific tasks: X, Y'){RESET}")
    instruction = input("> ").strip()
    if not instruction:
        print("No change.")
        return

    vision = open("VISION.md").read() if os.path.exists("VISION.md") else ""
    current = "\n".join(task_text(t) for t in get_open_tasks(current_tasks))

    print("\nGenerating new tasks...")
    raw = ollama(
        model=PLANNER_MODEL,
        system="You are a senior developer replanning a day's work. Be specific and practical. Return only a JSON array of task strings, nothing else.",
        user=(
            f"Project vision:\n{vision}\n\n"
            f"Current tasks for {tomorrow_str}:\n{current}\n\n"
            f"Pivot instruction: {instruction}\n\n"
            f"Generate 2–3 replacement tasks for {tomorrow_str}. "
            f"Return ONLY a JSON array like: [\"Task one\", \"Task two\"]"
        ),
    )

    start = raw.find("[")
    end   = raw.rfind("]") + 1
    try:
        new_tasks = json.loads(raw[start:end])
    except Exception:
        print(f"⚠  Could not parse model response. Raw:\n{raw}")
        return

    print(f"\n{GREEN}New tasks for tomorrow:{RESET}")
    for i, t in enumerate(new_tasks, 1):
        print(f"  {i}. {t}")

    confirm = input(f"\nSave these to ROADMAP.md? [y/n] ").strip().lower()
    if confirm == "y":
        update_roadmap_day(ROADMAP_PATH, tomorrow_str, new_tasks)
        print(f"{GREEN}✓ ROADMAP.md updated.{RESET}")
    else:
        print("Cancelled.")


# ─── Edit today ──────────────────────────────────────────────────────────────

def edit_today(today_str: str, current_tasks: list[str]) -> list[str]:
    open_tasks = get_open_tasks(current_tasks)
    print(f"\n{YELLOW}{BOLD}Edit today's tasks ({today_str}){RESET}")
    print("Current open tasks (press Enter to keep, type replacement, or 'x' to remove):\n")
    new_tasks = []
    for t in open_tasks:
        text = task_text(t)
        replacement = input(f"  '{text}'\n  → ").strip()
        if replacement.lower() == "x":
            continue
        elif replacement:
            new_tasks.append(replacement)
        else:
            new_tasks.append(text)

    add_more = input("\nAdd a new task? (or press Enter to skip)\n  → ").strip()
    if add_more:
        new_tasks.append(add_more)

    update_roadmap_day(ROADMAP_PATH, today_str, new_tasks)
    print(f"{GREEN}✓ Today's tasks updated.{RESET}\n")
    return [f"- [ ] {t}" for t in new_tasks]


# ─── Write approved tasks & mission ─────────────────────────────────────────

def write_approved(today_str: str, tasks: list[str]):
    os.makedirs(LOG_DIR, exist_ok=True)
    open_tasks = get_open_tasks(tasks)

    # Write today_approved.md — architect reads this
    with open(APPROVED_PATH, "w") as f:
        f.write(f"# Approved tasks for {today_str}\n\n")
        for t in open_tasks:
            f.write(f"{t}\n")
    print(f"{GREEN}✓ Approved tasks written to {APPROVED_PATH}{RESET}")

    # Write .roo-mission.md with the first task
    if open_tasks:
        first = task_text(open_tasks[0])
        remaining = [task_text(t) for t in open_tasks[1:]]
        rest_lines = "\n".join(f"  - {r}" for r in remaining) if remaining else "  (none)"
        rules_note = "See .roorules for coding conventions (file size, text selectability, changelog, release)." \
                     if os.path.exists(".roorules") else "See VISION.md for context."

        mission = (
            f"# Roo Mission — {today_str}\n\n"
            f"**Task:** {first}\n\n"
            f"**Remaining tasks today:**\n{rest_lines}\n\n"
            f"**Rules:** {rules_note}\n\n"
            "---\n"
            "_Written by standup.py. Complete this task, then run `python validate.py`._\n"
        )
        with open(".roo-mission.md", "w") as f:
            f.write(mission)
        print(f"{GREEN}✓ First task written to .roo-mission.md{RESET}")
        print(f"\n{BOLD}First task:{RESET} {first}")
        print(f"\n{DIM}Open VSCode, load .roo-mission.md in Roo, and start working.")
        print(f"When done, run: python validate.py{RESET}\n")
    else:
        print(f"{YELLOW}No open tasks for today — nothing to do!{RESET}")

    # Write daily log
    log_path = os.path.join(LOG_DIR, f"{today_str}.md")
    with open(log_path, "w") as f:
        f.write(f"# Standup log — {today_str}\n\n")
        f.write("## Approved tasks\n")
        for t in open_tasks:
            f.write(f"{t}\n")
        f.write("\n## Status\n- [ ] In progress\n")
    print(f"{DIM}✓ Log written to {log_path}{RESET}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="Path to the project folder (default: current dir)", default=None)
    args = parser.parse_args()

    project_flag = ""
    if args.project:
        project_path = os.path.abspath(args.project)
        if not os.path.isdir(project_path):
            print(f"⚠  Project folder not found: {project_path}")
            sys.exit(1)
        os.chdir(project_path)
        project_flag = f" --project {project_path}"

    project_root = os.getcwd()

    today    = date.today()
    tomorrow = today + timedelta(days=1)

    today_str    = today.isoformat()
    tomorrow_str = tomorrow.isoformat()

    if not os.path.exists(ROADMAP_PATH):
        print(f"⚠  No ROADMAP.md found. Run: python plan_week.py{project_flag}")
        sys.exit(1)

    roadmap = parse_roadmap(ROADMAP_PATH)

    today_tasks    = roadmap.get(today_str, [])
    tomorrow_tasks = roadmap.get(tomorrow_str, [])

    print_header(today)

    # ── Velocity snapshot (last 3 days) — shows before task list so you can
    #    see yesterday's error rate before committing to today's plan. ──────────
    _load_recs, _vel_report = _load_velocity()
    if _load_recs is not None:
        records = _load_recs(project_root)
        if records:
            _vel_report(records, days=3)
        else:
            print(f"  {DIM}(no velocity data yet — run work.py to start tracking){RESET}\n")

    print_tasks("📋 TODAY",    today_str,    today_tasks,    color=GREEN)
    print_tasks("📅 TOMORROW", tomorrow_str, tomorrow_tasks, color=BLUE,
                warn_if_under=MIN_DAY_TASKS)

    if not today_tasks:
        print(f"{YELLOW}No tasks planned for today in ROADMAP.md.{RESET}")
        print(f"Run `python plan_week.py{project_flag}` to generate a week's plan, or add tasks manually to ROADMAP.md.\n")
        sys.exit(0)

    while True:
        print_menu()
        choice = input("> ").strip().lower()

        if choice == "y":
            # Re-read in case edits were made
            roadmap      = parse_roadmap(ROADMAP_PATH)
            today_tasks  = roadmap.get(today_str, [])
            write_approved(today_str, today_tasks)
            break

        elif choice == "f":
            roadmap        = parse_roadmap(ROADMAP_PATH)
            tomorrow_tasks = roadmap.get(tomorrow_str, [])
            open_count     = len(get_open_tasks(tomorrow_tasks))
            needed         = max(0, MIN_DAY_TASKS - open_count)
            if needed == 0:
                print(f"{GREEN}Tomorrow already has {open_count} tasks — nothing to pull.{RESET}\n")
            else:
                print(f"\n{YELLOW}Pulling {needed} task(s) from backlog into tomorrow ({tomorrow_str})...{RESET}")
                moved = fill_tomorrow_from_backlog(ROADMAP_PATH, roadmap, tomorrow_str, needed)
                if not moved:
                    print(f"{YELLOW}No backlog tasks found to pull.{RESET}\n")
                else:
                    print(f"{GREEN}✓ Moved {len(moved)} task(s):{RESET}")
                    for t in moved:
                        print(f"  + {t}")
                    roadmap        = parse_roadmap(ROADMAP_PATH)
                    tomorrow_tasks = roadmap.get(tomorrow_str, [])
                    print()
                    print_tasks("📅 TOMORROW (updated)", tomorrow_str, tomorrow_tasks,
                                color=GREEN, warn_if_under=MIN_DAY_TASKS)

        elif choice == "p":
            roadmap      = parse_roadmap(ROADMAP_PATH)
            tomorrow_tasks = roadmap.get(tomorrow_str, [])
            pivot_tomorrow(tomorrow_str, tomorrow_tasks)
            # Refresh display after pivot
            roadmap        = parse_roadmap(ROADMAP_PATH)
            tomorrow_tasks = roadmap.get(tomorrow_str, [])
            print()
            print_tasks("📅 TOMORROW (updated)", tomorrow_str, tomorrow_tasks,
                        color=YELLOW, warn_if_under=MIN_DAY_TASKS)

        elif choice == "e":
            today_tasks = edit_today(today_str, today_tasks)
            print_tasks("📋 TODAY (updated)", today_str, today_tasks, color=GREEN)

        elif choice == "q":
            print("Exiting. Have a good day!")
            break

        else:
            print(f"{DIM}Type y, p, e, or q.{RESET}\n")


if __name__ == "__main__":
    main()
