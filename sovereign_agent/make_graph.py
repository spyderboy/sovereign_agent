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

History-aware enrichment (applied when logs/ dir exists):
  4. Prior outcomes — tasks previously marked "done" carry that status forward
     and are skipped in the next run.
  5. Error hints — tasks that failed before get their prior error codes and
     a hint string injected, giving workers more context on retry.
  6. Tier escalation — tasks that timed out on tier-1 get min_tier=2 so the
     orchestrator skips straight to the stronger model.
  7. Analyze errors — current `flutter analyze` errors for each file are
     injected as hints so workers know the exact current failure.

Usage:
    python make_graph.py --project ~/Code/astro_flux
    python make_graph.py --project ~/Code/astro_flux --out task_graph.json
    python make_graph.py --project ~/Code/astro_flux --show-stats
    python make_graph.py --project ~/Code/astro_flux --analyze-out analyze_output_3.txt
"""
import os
import re
import sys
import json
import argparse
import subprocess
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


# ── History & as-built loaders ────────────────────────────────────────────────

def _normalize(desc: str) -> str:
    """Collapse whitespace and lowercase for fuzzy task matching."""
    return re.sub(r"\s+", " ", desc).strip().lower()


def load_task_history(logs_dir: str) -> dict[str, dict]:
    """
    Read logs/task_traces.jsonl and return a map:
        normalized_task_description → {
            "outcome":     str,   # last summary outcome: done/timeout/budget/no_output/skipped
            "attempts":    int,
            "tiers_used":  list[int],
            "error_codes": list[str],   # union of error codes across all attempts
        }
    Only task_summary records determine outcome; attempt records supply error detail.
    """
    traces_path = os.path.join(logs_dir, "task_traces.jsonl")
    errors_path = os.path.join(logs_dir, "errors.jsonl")
    if not os.path.exists(traces_path):
        return {}

    summaries: dict[str, dict] = {}
    attempt_errors: dict[str, list[str]] = defaultdict(list)

    with open(traces_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = _normalize(rec.get("task", ""))
            if not key:
                continue
            rtype = rec.get("record_type", "")
            if rtype == "task_summary":
                # last summary wins (later runs override earlier ones)
                summaries[key] = {
                    "outcome":    rec.get("task_outcome", "unknown"),
                    "attempts":   rec.get("total_attempts", 0),
                    "tiers_used": [],
                }
            elif rtype == "attempt":
                tier = rec.get("tier")
                if tier:
                    summaries.setdefault(key, {"outcome": "unknown", "attempts": 0, "tiers_used": []})
                    if tier not in summaries[key]["tiers_used"]:
                        summaries[key]["tiers_used"].append(tier)

    # Layer in error codes from errors.jsonl
    if os.path.exists(errors_path):
        with open(errors_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = _normalize(rec.get("task", ""))
                if key:
                    attempt_errors[key].extend(rec.get("error_codes", []))

    # Merge error codes into summaries
    for key, errs in attempt_errors.items():
        if key in summaries:
            summaries[key]["error_codes"] = list(dict.fromkeys(errs))
        else:
            summaries[key] = {
                "outcome": "unknown", "attempts": 0,
                "tiers_used": [], "error_codes": list(dict.fromkeys(errs)),
            }

    # Ensure error_codes key always present
    for v in summaries.values():
        v.setdefault("error_codes", [])

    return summaries


def load_analyze_errors(project_root: str, analyze_out: str | None = None) -> dict[str, list[str]]:
    """
    Return a map of file_path → [error message strings] from flutter analyze.

    Tries in order:
      1. The file named by --analyze-out if provided and exists.
      2. The most recently modified analyze_output*.txt in project_root.
      3. Runs `flutter analyze` live (slow, ~30s).

    Only 'error' severity lines are included; warnings/infos are ignored.
    """
    raw = ""

    # 1. Explicit file
    if analyze_out:
        p = os.path.join(project_root, analyze_out) if not os.path.isabs(analyze_out) else analyze_out
        if os.path.exists(p):
            raw = open(p).read()

    # 2. Most recent analyze_output*.txt
    if not raw:
        candidates = sorted(
            Path(project_root).glob("analyze_output*.txt"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            raw = candidates[0].read_text()
            print(f"  Using cached analyze output: {candidates[0].name}")

    # 3. Live run
    if not raw:
        print("  Running flutter analyze (this may take ~30s)...")
        try:
            result = subprocess.run(
                ["flutter", "analyze", "--no-pub"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            raw = result.stdout + result.stderr
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"  ⚠  flutter analyze failed: {e}")
            return {}

    # Parse: only error lines, extract file path
    # Format: "  error • <message> • lib/foo/bar.dart:10:5 • error_code"
    file_errors: dict[str, list[str]] = defaultdict(list)
    error_line_re = re.compile(r"^\s+error\s+•\s+(.+?)\s+•\s+(lib/[\w/]+\.dart):\d+:\d+")
    for line in raw.splitlines():
        m = error_line_re.match(line)
        if m:
            msg, fpath = m.group(1), m.group(2)
            file_errors[fpath].append(msg)

    return dict(file_errors)


def enrich_tasks(
    tasks: list[dict],
    history: dict[str, dict],
    analyze_errors: dict[str, list[str]],
) -> dict[str, int]:
    """
    Mutate tasks in-place with history-derived fields:
      - status:           "done" if previously completed successfully
      - prior_outcome:    last known outcome string
      - prior_error_codes: list of Dart analyzer error codes seen before
      - hint:             plain-English context string for the worker
      - min_tier:         1 (default) or 2 if task burned through tier-1 repeatedly

    Returns a summary dict with counts for reporting.
    """
    counts = {"carried_done": 0, "escalated": 0, "hinted": 0}

    for t in tasks:
        key = _normalize(t["description"])
        hist = history.get(key)

        # Collect current analyze errors for this task's files
        current_errors: list[str] = []
        for f in t.get("files", []):
            current_errors.extend(analyze_errors.get(f, []))

        hint_parts: list[str] = []

        if hist:
            outcome = hist["outcome"]
            error_codes = hist["error_codes"]

            t["prior_outcome"] = outcome
            if error_codes:
                t["prior_error_codes"] = error_codes

            # Carry forward done status
            if outcome == "done":
                t["status"] = "done"
                counts["carried_done"] += 1
                continue  # no need to add hints for done tasks

            # Escalate to tier-2 if tier-1 consistently failed
            tiers = hist.get("tiers_used", [])
            attempts = hist.get("attempts", 0)
            if attempts >= 3 and (not tiers or max(tiers) < 2):
                t["min_tier"] = 2
                counts["escalated"] += 1
            else:
                t.setdefault("min_tier", 1)

            # Hint from prior error codes
            if error_codes:
                hint_parts.append(
                    f"Previous attempts produced these Dart analyzer errors: "
                    f"{', '.join(error_codes)}. "
                    f"Avoid reproducing these patterns."
                )
        else:
            t.setdefault("min_tier", 1)

        # Hint from current as-built analyze errors in target files
        if current_errors:
            # Deduplicate, cap at 5 to keep hints compact
            deduped = list(dict.fromkeys(current_errors))[:5]
            hint_parts.append(
                f"Current flutter analyze errors in target file(s): "
                + " | ".join(deduped)
            )

        if hint_parts:
            t["hint"] = " ".join(hint_parts)
            counts["hinted"] += 1

    return counts


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
    parser.add_argument("--project",      default=None,
                        help="Path to project root (default: current dir)")
    parser.add_argument("--out",          default="task_graph.json",
                        help="Output filename (default: task_graph.json)")
    parser.add_argument("--logs-dir",     default=None,
                        help="Path to logs dir (default: <sovereign_agent>/logs)")
    parser.add_argument("--analyze-out",  default=None,
                        help="Cached flutter analyze output file to read instead of running live")
    parser.add_argument("--no-history",   action="store_true",
                        help="Skip history enrichment even if logs/ exists")
    parser.add_argument("--show-stats",   action="store_true",
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

    tasks = build_graph(raw)
    dupes = len(raw) - len(tasks)
    if dupes:
        print(f"  {dupes} duplicate(s) removed → {len(tasks)} unique tasks")

    # ── History-aware enrichment ──────────────────────────────────────────────
    if not args.no_history:
        # Locate logs dir: explicit flag → sovereign_agent sibling → skip
        logs_dir = args.logs_dir
        if not logs_dir:
            # Assume sovereign_agent is a sibling of the project, or find via __file__
            candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            if os.path.isdir(candidate):
                logs_dir = candidate

        if logs_dir and os.path.isdir(logs_dir):
            print(f"Loading task history from {logs_dir}...")
            history = load_task_history(logs_dir)
            print(f"  {len(history)} prior task records found")

            print(f"Loading as-built analyze errors from {project_root}...")
            analyze_errors = load_analyze_errors(project_root, args.analyze_out)
            files_with_errors = len(analyze_errors)
            total_errors = sum(len(v) for v in analyze_errors.values())
            print(f"  {files_with_errors} files with current errors ({total_errors} total)")

            counts = enrich_tasks(tasks, history, analyze_errors)
            print(
                f"  Enrichment: {counts['carried_done']} carried done, "
                f"{counts['escalated']} escalated to tier-2, "
                f"{counts['hinted']} tasks got hints"
            )
        else:
            print("  No logs/ dir found — skipping history enrichment")

    # ── Stats & output ────────────────────────────────────────────────────────
    pending_tasks = [t for t in tasks if t.get("status") != "done"]
    edges  = sum(len(t["depends_on"]) for t in tasks)
    layers = compute_layers(tasks)

    graph = {
        "generated":       date.today().isoformat(),
        "project":         os.path.basename(project_root),
        "total":           len(tasks),
        "pending":         len(pending_tasks),
        "tasks":           tasks,
    }

    with open(out_path, "w") as fh:
        json.dump(graph, fh, indent=2)
    print(
        f"✓ {out_path} written — "
        f"{len(pending_tasks)} pending / {len(tasks)} total, "
        f"{edges} dependency edges"
    )

    if args.show_stats:
        print(f"\nParallelism analysis (pending tasks only):")
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
