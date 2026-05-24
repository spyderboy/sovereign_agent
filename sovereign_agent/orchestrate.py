"""
orchestrate.py — Cloud sprint orchestrator

Reads task_graph.json, tracks per-task state in Firestore, and drives the
two-tier pipeline by publishing to Pub/Sub:

  tasks-tier1  →  7B workers   (fast, cheap)
  tasks-tier2  →  32B workers  (only for tier-1 failures; Claude escalation
                                is handled inline by the tier-2 worker itself)

Workers publish results back to:
  task-results  →  this orchestrator

A "failed" result from a tier-2 worker means BOTH the 32B model AND Claude
were tried — the orchestrator marks that task BLOCKED with no further action.

Usage:
  # Start a sprint run (reads task_graph.json from project root):
  python orchestrate.py --project ~/Code/astro_flux

  # Dry run — print what would be dispatched without touching Pub/Sub:
  python orchestrate.py --project ~/Code/astro_flux --dry-run

  # Check current run status:
  python orchestrate.py --project ~/Code/astro_flux --status

  # Reset all task states to pending (re-run the whole sprint):
  python orchestrate.py --project ~/Code/astro_flux --reset

Environment variables:
  FIRESTORE_PROJECT_ID   GCP project (required)
  PUBSUB_PROJECT_ID      GCP project for Pub/Sub (defaults to FIRESTORE_PROJECT_ID)
  GCP_PROJECT            Fallback project ID
"""
import os
import sys
import json
import time
import argparse
import threading
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

GCP_PROJECT = (
    os.getenv("FIRESTORE_PROJECT_ID")
    or os.getenv("PUBSUB_PROJECT_ID")
    or os.getenv("GCP_PROJECT", "")
)

# ── Pub/Sub topic names ────────────────────────────────────────────────────────
TOPIC_TIER1   = "tasks-tier1"
TOPIC_TIER2   = "tasks-tier2"
TOPIC_RESULTS = "task-results"
SUB_RESULTS   = "task-results-orchestrator"

# ── Firestore collection ───────────────────────────────────────────────────────
FS_COLLECTION = "sprint_tasks"

# ── Task statuses ──────────────────────────────────────────────────────────────
# tier-2 workers escalate to Claude inline, so there is no separate CLAUDE_RUNNING
# state. A "failed" from tier-2 already means 32B + Claude both tried and failed.
PENDING       = "pending"
TIER1_RUNNING = "tier1_running"
TIER1_FAILED  = "tier1_failed"
TIER2_RUNNING = "tier2_running"
TIER2_FAILED  = "tier2_failed"
DONE          = "done"
BLOCKED       = "blocked"   # 32B + Claude both failed


# ─── Firestore helpers ─────────────────────────────────────────────────────────

def _fs_client():
    from google.cloud import firestore
    return firestore.Client(project=GCP_PROJECT)


def _run_id(project_root: str) -> str:
    """Stable run ID: project name + today's date."""
    name = os.path.basename(project_root.rstrip("/"))
    return f"{name}_{date.today().isoformat()}"


def load_state(db, run_id: str) -> dict[int, dict]:
    """Load all task states for this run from Firestore. Returns {task_id: doc}."""
    col = db.collection(FS_COLLECTION).document(run_id).collection("tasks")
    return {int(doc.id): doc.to_dict() for doc in col.stream()}


def save_task_state(db, run_id: str, task_id: int, data: dict):
    """Upsert a task state document."""
    ref = (db.collection(FS_COLLECTION)
             .document(run_id)
             .collection("tasks")
             .document(str(task_id)))
    ref.set(data, merge=True)


# ─── Pub/Sub helpers ───────────────────────────────────────────────────────────

def _ps_client():
    from google.cloud import pubsub_v1
    return pubsub_v1.PublisherClient()


def _ps_sub_client():
    from google.cloud import pubsub_v1
    return pubsub_v1.SubscriberClient()


def topic_path(project: str, topic: str) -> str:
    return f"projects/{project}/topics/{topic}"


def sub_path(project: str, sub: str) -> str:
    return f"projects/{project}/subscriptions/{sub}"


def ensure_topic(publisher, project: str, name: str, dry_run: bool):
    path = topic_path(project, name)
    if dry_run:
        print(f"  [dry] topic: {path}")
        return
    try:
        publisher.create_topic(request={"name": path})
        print(f"  ✓ Created topic {name}")
    except Exception:
        pass  # already exists


def ensure_subscription(sub_client, project: str, topic: str, sub: str, dry_run: bool):
    t = topic_path(project, topic)
    s = sub_path(project, sub)
    if dry_run:
        print(f"  [dry] subscription: {s} → {t}")
        return
    try:
        sub_client.create_subscription(request={"name": s, "topic": t})
        print(f"  ✓ Created subscription {sub}")
    except Exception:
        pass  # already exists


def publish(publisher, project: str, topic: str, payload: dict, dry_run: bool):
    import json as _j
    data = _j.dumps(payload).encode()
    if dry_run:
        print(f"  [dry] → {topic}: task {payload['task_id']}")
        return
    publisher.publish(topic_path(project, topic), data)


# ─── Core orchestration loop ───────────────────────────────────────────────────

def dispatch_ready(
    tasks: list[dict],
    state:  dict[int, dict],
    db,
    publisher,
    run_id: str,
    project: str,
    dry_run: bool,
) -> int:
    """
    Find tasks whose deps are all DONE and whose own status is PENDING.
    Publish them to tasks-tier1.  Returns number dispatched.
    """
    done_ids   = {tid for tid, s in state.items() if s.get("status") == DONE}
    dispatched = 0

    for t in tasks:
        tid = t["id"]
        if state.get(tid, {}).get("status", PENDING) != PENDING:
            continue
        if all(dep in done_ids for dep in t["depends_on"]):
            payload = {
                "task_id":     tid,
                "description": t["description"],
                "files":       t["files"],
                "run_id":      run_id,
                "tier":        1,
            }
            publish(publisher, project, TOPIC_TIER1, payload, dry_run)
            new_state = {"status": TIER1_RUNNING, "dispatched_at": datetime.utcnow().isoformat()}
            state[tid] = {**state.get(tid, {}), **new_state}
            save_task_state(db, run_id, tid, new_state)
            print(f"  → [{tid}] tier1: {t['short'][:70]}")
            dispatched += 1

    return dispatched



def print_status(tasks: list[dict], state: dict[int, dict]):
    counts = {
        PENDING:       0, TIER1_RUNNING: 0, TIER1_FAILED: 0,
        TIER2_RUNNING: 0, TIER2_FAILED:  0,
        DONE:          0, BLOCKED:       0,
    }
    for t in tasks:
        s = state.get(t["id"], {}).get("status", PENDING)
        counts[s] = counts.get(s, 0) + 1

    total = len(tasks)
    done  = counts[DONE]
    print(f"\n  Sprint progress: {done}/{total} done "
          f"({done/total*100:.0f}%)")
    for status, count in counts.items():
        if count:
            bar = "█" * min(count, 30)
            print(f"  {status:<18} {count:3d}  {bar}")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sovereign sprint orchestrator")
    parser.add_argument("--project",  default=None,
                        help="Project root (default: cwd)")
    parser.add_argument("--graph",    default="task_graph.json",
                        help="Path to task_graph.json (relative to --project)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print what would be dispatched without touching Pub/Sub")
    parser.add_argument("--status",   action="store_true",
                        help="Print current run status and exit")
    parser.add_argument("--reset",    action="store_true",
                        help="Reset all task states to pending and restart")
    args = parser.parse_args()

    if not GCP_PROJECT:
        print("ERROR: Set FIRESTORE_PROJECT_ID in .env or environment.")
        sys.exit(1)

    project_root = os.path.abspath(args.project) if args.project else os.getcwd()
    graph_path   = os.path.join(project_root, args.graph)

    if not os.path.exists(graph_path):
        print(f"⚠  task_graph.json not found at {graph_path}")
        print("   Run: python make_graph.py --project <project>")
        sys.exit(1)

    with open(graph_path) as fh:
        graph = json.load(fh)

    tasks       = graph["tasks"]
    tasks_by_id = {t["id"]: t for t in tasks}
    # Ensure 'short' key exists (make_graph writes it, but guard anyway)
    for t in tasks:
        if "short" not in t:
            t["short"] = t["description"][:80]

    run_id = _run_id(project_root)
    print(f"Run ID  : {run_id}")
    print(f"Tasks   : {len(tasks)}")
    print(f"Project : {GCP_PROJECT}")
    print(f"Dry run : {args.dry_run}")

    db        = _fs_client()
    state     = load_state(db, run_id)
    publisher = _ps_client()
    sub_client = _ps_sub_client()

    if args.status:
        print_status(tasks, state)
        return

    if args.reset:
        print("\nResetting all task states to pending...")
        state = {}
        for t in tasks:
            save_task_state(db, run_id, t["id"], {"status": PENDING})
        print(f"✓ {len(tasks)} tasks reset.")

    # ── Ensure Pub/Sub infrastructure exists ──────────────────────────────────
    print("\n→ Ensuring Pub/Sub topics and subscriptions...")
    ensure_topic(publisher, GCP_PROJECT, TOPIC_TIER1,   args.dry_run)
    ensure_topic(publisher, GCP_PROJECT, TOPIC_TIER2,   args.dry_run)
    ensure_topic(publisher, GCP_PROJECT, TOPIC_RESULTS, args.dry_run)
    ensure_subscription(sub_client, GCP_PROJECT, TOPIC_RESULTS,
                        SUB_RESULTS, args.dry_run)

    # ── Initial dispatch: fire all tasks with no unresolved deps ──────────────
    print("\n→ Dispatching initially-ready tasks...")
    n = dispatch_ready(tasks, state, db, publisher, run_id, GCP_PROJECT, args.dry_run)
    print(f"  {n} tasks dispatched to tier1.")

    if args.dry_run:
        print_status(tasks, state)
        return

    # ── Result loop: poll Firestore directly ──────────────────────────────────
    # Workers write results straight to Firestore — no Pub/Sub round-trip needed.
    # We poll every few seconds, detect result_status appearing, and react.
    print("\n→ Polling Firestore for results (Ctrl-C to stop)...\n")

    last_orphan_check = time.time()

    def _process_result(tid: int, doc: dict):
        """Handle a completed task doc — update state, dispatch dependents/escalate."""
        result  = doc.get("result_status")   # "done" or "failed"
        tier    = doc.get("completed_tier", 1)
        task    = tasks_by_id.get(tid)
        if task is None or result is None:
            return
        cur_status = state.get(tid, {}).get("status", PENDING)
        # Skip if already processed
        if cur_status in (DONE, BLOCKED, TIER1_FAILED, TIER2_RUNNING):
            return

        print(f"  ← [{tid}] tier{tier} {result}: {task['short'][:60]}")

        if result == "done":
            new_state = {"status": DONE, "completed_tier": tier,
                         "completed_at": datetime.utcnow().isoformat()}
            state[tid] = {**state.get(tid, {}), **new_state}
            save_task_state(db, run_id, tid, new_state)
            # Unlock dependents
            done_ids = {t for t, s in state.items() if s.get("status") == DONE}
            for t in tasks_by_id.values():
                if state.get(t["id"], {}).get("status", PENDING) == PENDING:
                    if all(dep in done_ids for dep in t["depends_on"]):
                        p = {"task_id": t["id"], "description": t["description"],
                             "files": t["files"], "run_id": run_id, "tier": 1}
                        publish(publisher, GCP_PROJECT, TOPIC_TIER1, p, args.dry_run)
                        ns = {"status": TIER1_RUNNING,
                              "dispatched_at": datetime.utcnow().isoformat()}
                        state[t["id"]] = {**state.get(t["id"], {}), **ns}
                        save_task_state(db, run_id, t["id"], ns)
                        print(f"  → [{t['id']}] tier1 (unblocked): {t['short'][:55]}")

        elif result == "failed":
            if tier == 1:
                new_state = {"status": TIER1_FAILED,
                             "tier1_failed_at": datetime.utcnow().isoformat()}
                state[tid] = {**state.get(tid, {}), **new_state}
                save_task_state(db, run_id, tid, new_state)
                p = {"task_id": tid, "description": task["description"],
                     "files": task["files"], "run_id": run_id, "tier": 2}
                publish(publisher, GCP_PROJECT, TOPIC_TIER2, p, args.dry_run)
                ns = {"status": TIER2_RUNNING,
                      "dispatched_at_t2": datetime.utcnow().isoformat()}
                state[tid] = {**state[tid], **ns}
                save_task_state(db, run_id, tid, ns)
                print(f"  → [{tid}] tier2 (escalated): {task['short'][:55]}")
            elif tier == 2:
                new_state = {"status": BLOCKED,
                             "tier2_failed_at": datetime.utcnow().isoformat(),
                             "blocked_at":      datetime.utcnow().isoformat(),
                             "error":           doc.get("error", "")}
                state[tid] = {**state.get(tid, {}), **new_state}
                save_task_state(db, run_id, tid, new_state)
                print(f"  ✗ [{tid}] BLOCKED: {task['short'][:55]}")

    try:
        while True:
            # ── Reload Firestore state and process any new results ────────────
            fresh = load_state(db, run_id)
            for tid, doc in fresh.items():
                if doc.get("result_status"):
                    _process_result(tid, doc)
                # Sync fresh state into local dict (without overwriting our updates)
                if tid not in state:
                    state[tid] = doc

            # ── Check if all done ─────────────────────────────────────────────
            terminal = (DONE, BLOCKED)
            remaining = [t for t in tasks
                         if state.get(t["id"], {}).get("status", PENDING)
                         not in terminal]
            if not remaining:
                print("\n✓ All tasks complete!")
                print_status(tasks, state)
                break

            # ── Dispatch any newly-ready pending tasks ────────────────────────
            dispatch_ready(tasks, state, db, publisher,
                           run_id, GCP_PROJECT, args.dry_run)

            # ── Reset orphaned running tasks (age > 15 min) ───────────────────
            now = time.time()
            if now - last_orphan_check > 120:
                last_orphan_check = now
                for t in tasks:
                    tid = t["id"]
                    s = state.get(tid, {})
                    for running_status, ts_key in [
                        (TIER1_RUNNING, "dispatched_at"),
                        (TIER2_RUNNING, "dispatched_at_t2"),
                    ]:
                        if s.get("status") == running_status:
                            ts = s.get(ts_key, "")
                            if ts:
                                try:
                                    age = (datetime.utcnow() -
                                           datetime.fromisoformat(ts)).total_seconds()
                                    if age > 1800:
                                        reset_to = (PENDING if running_status == TIER1_RUNNING
                                                    else TIER1_FAILED)
                                        save_task_state(db, run_id, tid,
                                                        {"status": reset_to,
                                                         "result_status": None})
                                        state[tid] = {**s, "status": reset_to}
                                        print(f"  ↺  [{tid}] orphaned "
                                              f"{running_status} → {reset_to}")
                                except Exception:
                                    pass

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n\nInterrupted. Current status:")
        print_status(tasks, state)



if __name__ == "__main__":
    main()
