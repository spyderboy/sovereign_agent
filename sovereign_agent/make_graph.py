"""
make_graph.py — Generate task_graph.json from ROADMAP.md

Reads every unchecked task from ROADMAP.md and builds a dependency DAG so the
cloud orchestrator knows which tasks are ready to run in parallel and which must
wait for others to finish first.

Dependency rules (applied in order):
  1. Deduplication — identical task descriptions are collapsed to one node.
  2. File-sequential — tasks that touch the same .dart file run in ROADMAP order.
     Parallel writes to the same file would stomp each other.
  3. Entity-name matching — if task A says "Create/Define FooBar" and task B
     mentions "FooBar" without creating it, B depends on A.

Usage:
    python make_graph.py --project ~/Code/astro_flux
    python make_graph.py --project ~/Code/astro_flux --out task_graph.json
    python make_graph.py --project ~/Code/astro_flux --show-stats
"""
import os
import re
import sys
import json
import argparse
from datetime import date
from pathlib import Path
from collections import defaultdict

# ── Patterns ──────────────────────────────────────────────────────────────────
# Dart file path anywhere in the task description
_FILE_RE = re.compile(r"lib/[\w/]+\.dart")

# "done when:" suffix — strip for cleaner display but keep full text in record
_DONE_WHEN_RE = re.compile(r"\s*—\s*done when:.*$", re.IGNORECASE)

# Verbs that signal "this task CREATES the named entity"
_CREATE_VERBS = re.compile(
    r"^(?:create|define|add|implement|build|write|introduce|establish)\b",
    re.IGNORECASE,
)

# Extract a PascalCase or snake_case entity name from the start of a task
# e.g. "Create CombatScoreWidget ..." → "CombatScoreWidget"
_ENTITY_RE = re.compile(
    r"(?:Create|Define|Add|Implement|Build|Write)\s+"
    r"([A-Z][A-Za-z0-9]+(?:Widget|Component|Handler|Service|Notifier|Provider|"
    r"Resolver|Manager|Controller|Event|Model|System|Observer|Animator|"
    r"Mixin|Helper|Util|State|Screen|Page|View|Button|Dialog|Sheet|"
    r"Repository|Gateway|Factory|Builder|Delegate|Strategy)?)",
    re.IGNORECASE,
)


def parse_roadmap(path: str) -> list[str]:
    """Return all unchecked task descriptions from ROADMAP.md."""
    tasks = []
    with open(path) as fh:
        for line in fh:
            if line.startswith("- [ ]"):
                desc = line[5:].strip()
                tasks.append(desc)
    return tasks


def extract_files(desc: str) -> list[str]:
    """Return all lib/...dart paths mentioned in a task description."""
    return list(dict.fromkeys(_FILE_RE.findall(desc)))  # deduplicated, ordered


def extract_entity(desc: str) -> str | None:
    """Return the PascalCase entity this task creates, or None."""
    m = _ENTITY_RE.search(desc)
    return m.group(1) if m else None


def short(desc: str) -> str:
    """Strip 'done when:' clause for display."""
    return _DONE_WHEN_RE.sub("", desc).strip()


def build_graph(raw_tasks: list[str]) -> list[dict]:
    """
    Deduplicate tasks and compute depends_on edges.
    Returns a list of task dicts (ids are 0-indexed, stable).
    """
    # ── 1. Deduplicate (keep first occurrence, preserve order) ────────────────
    seen: set[str] = set()
    unique: list[str] = []
    for t in raw_tasks:
        key = short(t).lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)

    n = len(unique)
    tasks = []
    for i, desc in enumerate(unique):
        tasks.append({
            "id":          i,
            "description": desc,
            "short":       short(desc),
            "files":       extract_files(desc),
            "entity":      extract_entity(desc),
            "depends_on":  [],
            "status":      "pending",
            "tier_history": [],
        })

    # ── 2. File-sequential edges ──────────────────────────────────────────────
    # For each file, record the tasks that touch it in ROADMAP order.
    # Each task depends on the immediately preceding task on the same file.
    file_to_tasks: dict[str, list[int]] = defaultdict(list)
    for t in tasks:
        for f in t["files"]:
            file_to_tasks[f].append(t["id"])

    for f, ids in file_to_tasks.items():
        for prev, curr in zip(ids, ids[1:]):
            if prev not in tasks[curr]["depends_on"]:
                tasks[curr]["depends_on"].append(prev)

    # ── 3. Entity-name matching ───────────────────────────────────────────────
    # Build a map: entity_name → task_id that CREATES it.
    entity_creator: dict[str, int] = {}
    for t in tasks:
        if t["entity"]:
            name = t["entity"].lower()
            if name not in entity_creator:
                entity_creator[name] = t["id"]

    # Any task that mentions an entity name (but doesn't create it itself)
    # depends on the creator task.
    for t in tasks:
        for name, creator_id in entity_creator.items():
            if creator_id == t["id"]:
                continue  # don't self-depend
            # Check if entity appears in the description (case-insensitive)
            if re.search(r"\b" + re.escape(name) + r"\b", t["short"], re.IGNORECASE):
                if creator_id not in t["depends_on"]:
                    # Only add if creator appears BEFORE this task in ROADMAP order
                    if creator_id < t["id"]:
                        t["depends_on"].append(creator_id)

    # Sort depends_on for deterministic output
    for t in tasks:
        t["depends_on"].sort()

    # ── Strip internal-only 'entity' key from final output ────────────────────
    for t in tasks:
        del t["entity"]

    return tasks


def compute_layers(tasks: list[dict]) -> list[list[int]]:
    """
    Topological layering: layer 0 has no dependencies, layer N+1 has all
    deps in layers ≤ N.  Useful for understanding parallelism potential.
    """
    id_to_layer: dict[int, int] = {}
    remaining = list(range(len(tasks)))
    layer = 0

    while remaining:
        ready = []
        for tid in remaining:
            deps = tasks[tid]["depends_on"]
            if all(d in id_to_layer for d in deps):
                ready.append(tid)
        if not ready:
            # Cycle or unresolvable — assign remaining to current layer
            for tid in remaining:
                id_to_layer[tid] = layer
            break
        for tid in ready:
            id_to_layer[tid] = layer
        remaining = [t for t in remaining if t not in id_to_layer]
        layer += 1

    layers: list[list[int]] = []
    for i in range(layer + 1):
        group = [tid for tid, l in id_to_layer.items() if l == i]
        if group:
            layers.append(sorted(group))
    return layers


def main():
    parser = argparse.ArgumentParser(
        description="Generate task_graph.json from ROADMAP.md"
    )
    parser.add_argument("--project",    default=None,
                        help="Path to project root (default: current dir)")
    parser.add_argument("--out",        default="task_graph.json",
                        help="Output filename (default: task_graph.json)")
    parser.add_argument("--show-stats", action="store_true",
                        help="Print dependency and parallelism stats")
    args = parser.parse_args()

    project_root = os.path.abspath(args.project) if args.project else os.getcwd()
    roadmap_path = os.path.join(project_root, "ROADMAP.md")
    out_path     = os.path.join(project_root, args.out)

    if not os.path.exists(roadmap_path):
        print(f"⚠  ROADMAP.md not found at {roadmap_path}")
        sys.exit(1)

    print(f"Reading {roadmap_path}...")
    raw = parse_roadmap(roadmap_path)
    print(f"  {len(raw)} unchecked tasks found")

    tasks  = build_graph(raw)
    dupes  = len(raw) - len(tasks)
    edges  = sum(len(t["depends_on"]) for t in tasks)
    layers = compute_layers(tasks)

    if dupes:
        print(f"  {dupes} duplicate(s) removed → {len(tasks)} unique tasks")

    graph = {
        "generated":   date.today().isoformat(),
        "project":     os.path.basename(project_root),
        "total":       len(tasks),
        "tasks":       tasks,
    }

    with open(out_path, "w") as fh:
        json.dump(graph, fh, indent=2)
    print(f"✓ {out_path} written ({len(tasks)} tasks, {edges} dependency edges)")

    if args.show_stats:
        print(f"\nParallelism analysis:")
        print(f"  Dependency layers : {len(layers)}")
        print(f"  Max parallel      : {max(len(l) for l in layers)} tasks")
        print(f"  Avg parallel      : {len(tasks)/len(layers):.1f} tasks/layer")
        print(f"\nLayer sizes:")
        for i, layer in enumerate(layers):
            bar = "█" * min(len(layer), 40)
            print(f"  Layer {i:2d}  ({len(layer):3d} tasks)  {bar}")
        print(f"\nMost-depended-on tasks:")
        dep_counts: dict[int, int] = defaultdict(int)
        for t in tasks:
            for d in t["depends_on"]:
                dep_counts[d] += 1
        for tid, count in sorted(dep_counts.items(), key=lambda x: -x[1])[:5]:
            print(f"  [{tid:3d}] {count} dependents — {tasks[tid]['short'][:70]}")


if __name__ == "__main__":
    main()
