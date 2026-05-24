"""
worker.py — Single-task cloud worker

Subscribes to one Pub/Sub tier queue, pulls a task, executes it, then
publishes the result back to task-results so the orchestrator can unlock
dependents or mark the task blocked.

Two worker tiers:
  WORKER_TIER=1  →  tries the 7B model only
  WORKER_TIER=2  →  tries the 32B model; if that fails, escalates to Claude
                    inline before reporting back (no separate Claude queue)

Claude escalation is intentionally kept inside the tier-2 worker so the
orchestrator stays simple — a "failed" result from tier-2 means BOTH 32B
and Claude were tried and neither succeeded.

Usage:
  WORKER_TIER=1 python worker.py --project ~/Code/astro_flux
  WORKER_TIER=2 python worker.py --project ~/Code/astro_flux

The worker exits after completing one task. On GCP, the Cloud Run Job
runner will restart it for the next task in the queue.
"""
import os
import sys
import json
import time
import tempfile
import argparse
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ── Work.py integration ───────────────────────────────────────────────────────
# Add sovereign_agent dir to path so we can import from work.py
sys.path.insert(0, os.path.dirname(__file__))

GCP_PROJECT = (
    os.getenv("FIRESTORE_PROJECT_ID")
    or os.getenv("PUBSUB_PROJECT_ID")
    or os.getenv("GCP_PROJECT", "")
)

WORKER_TIER = int(os.getenv("WORKER_TIER", "1"))   # 1 or 2

TOPIC_MAP = {1: "tasks-tier1", 2: "tasks-tier2"}
TOPIC_RESULTS = "task-results"
SUB_MAP   = {1: "tasks-tier1-worker", 2: "tasks-tier2-worker"}

# How long to wait for a message before giving up (seconds)
PULL_TIMEOUT = int(os.getenv("PULL_TIMEOUT_S", "300"))


# ─── Pub/Sub helpers ───────────────────────────────────────────────────────────

def topic_path(project: str, topic: str) -> str:
    return f"projects/{project}/topics/{topic}"


def sub_path(project: str, sub: str) -> str:
    return f"projects/{project}/subscriptions/{sub}"


def ensure_subscription(project: str, topic_name: str, sub_name: str):
    from google.cloud import pubsub_v1
    sub_client = pubsub_v1.SubscriberClient()
    t = topic_path(project, topic_name)
    s = sub_path(project, sub_name)
    try:
        sub_client.create_subscription(request={"name": s, "topic": t})
    except Exception:
        pass  # already exists
    return sub_client, s


def pull_one(project: str, sub_name: str, timeout_s: int) -> tuple[dict | None, object | None]:
    """
    Synchronous pull: wait up to timeout_s for one message.
    Returns (payload_dict, message) or (None, None) if nothing arrived.
    """
    from google.cloud import pubsub_v1
    sub_client = pubsub_v1.SubscriberClient()
    s = sub_path(project, sub_name)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            response = sub_client.pull(
                request={"subscription": s, "max_messages": 1},
                timeout=min(30, deadline - time.time()),
            )
        except Exception:
            time.sleep(2)
            continue
        if response.received_messages:
            msg = response.received_messages[0]
            try:
                payload = json.loads(msg.message.data.decode())
                return payload, (sub_client, s, msg.ack_id)
            except Exception as exc:
                # Bad message — ack and skip
                sub_client.acknowledge(
                    request={"subscription": s, "ack_ids": [msg.ack_id]}
                )
                print(f"  ⚠  Malformed message: {exc}")
        time.sleep(2)

    return None, None


def ack(handle):
    sub_client, s, ack_id = handle
    sub_client.acknowledge(request={"subscription": s, "ack_ids": [ack_id]})


def write_result(project: str, run_id: str, task_id: int,
                 status: str, tier: int, duration_s: float, error: str):
    """Write task result directly to Firestore — reliable, no Pub/Sub needed."""
    import socket
    from google.cloud import firestore
    db = firestore.Client(project=project)
    ref = (db.collection("sprint_tasks")
             .document(run_id)
             .collection("tasks")
             .document(str(task_id)))
    data = {
        "result_status": status,      # "done" or "failed"
        "completed_tier": tier,
        "duration_s":     round(duration_s, 1),
        "error":          error,
        "worker_at":      datetime.utcnow().isoformat(),
        "worker_host":    socket.gethostname(),
    }
    ref.set(data, merge=True)
    print(f"  ✓ Result written to Firestore ({status})")


# ─── Tier execution ────────────────────────────────────────────────────────────

def _run_7b(task_desc: str, project_root: str) -> tuple[bool, str]:
    """Try the 7B model. Returns (success, error_summary)."""
    import work
    log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix="sovereign_t1_")
    os.close(log_fd)
    try:
        result = work.run_task(
            task           = task_desc,
            project_root   = project_root,
            log_file       = log_path,
            max_tier_idx   = 1,   # tier 1 only
            start_tier_idx = 0,
        )
        success = result is True
        return success, "" if success else str(result)
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


def _run_32b(task_desc: str, project_root: str) -> tuple[bool, str]:
    """Try the 32B model. Returns (success, error_summary)."""
    import work
    log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix="sovereign_t2_")
    os.close(log_fd)
    try:
        result = work.run_task(
            task           = task_desc,
            project_root   = project_root,
            log_file       = log_path,
            max_tier_idx   = 2,
            start_tier_idx = 1,   # skip 7B, start at 32B
        )
        success = result is True
        return success, "" if success else str(result)
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


def _run_claude(task_desc: str, project_root: str, files: list[str]) -> tuple[bool, str]:
    """
    Inline Claude escalation — called by the tier-2 worker when 32B fails.
    Returns (success, error_summary).
    """
    import work
    try:
        file_contents = work.read_files(files, project_root)
        changes, ok = work._escalate_to_claude(
            task          = task_desc,
            file_contents = file_contents,
            errors        = [],
            project_root  = project_root,
            is_test       = False,
        )
        if not changes:
            return False, "Claude returned no changes"

        written, pat_errs = work.write_changes(changes, project_root, test_only=False)
        if pat_errs:
            return False, f"Pattern errors: {pat_errs}"

        passed, output = work.validate()
        if not passed:
            # Restore the files we just wrote so the repo stays clean
            originals = {}
            for f in written:
                fpath = os.path.join(project_root, f)
                if os.path.exists(fpath):
                    originals[f] = open(fpath).read()
            work.restore_files(originals, project_root)
            return False, output[:300]

        return True, ""
    except Exception as exc:
        return False, str(exc)


def run_tier1(task_desc: str, project_root: str, files: list[str]) -> tuple[bool, str]:
    """Entry point for tier-1 workers — 7B only."""
    return _run_7b(task_desc, project_root)


def run_tier2(task_desc: str, project_root: str, files: list[str]) -> tuple[bool, str]:
    """
    Entry point for tier-2 workers.

    Tries 32B first.  If that fails, escalates to Claude inline so the
    orchestrator never needs to dispatch a separate Claude queue entry.
    A "failed" result from this function means BOTH models were tried.
    """
    print("  → Attempting 32B model...")
    success, error = _run_32b(task_desc, project_root)
    if success:
        return True, ""

    print(f"  ✗ 32B failed ({error[:120]}). Escalating to Claude inline...")
    success, error = _run_claude(task_desc, project_root, files)
    if success:
        print("  ✓ Claude succeeded.")
        return True, ""

    print(f"  ✗ Claude also failed ({error[:120]}).")
    return False, f"32B+Claude both failed. Last error: {error}"


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sovereign single-task cloud worker")
    parser.add_argument("--project", default=None,
                        help="Project root (default: PROJECT_DIR env or cwd)")
    parser.add_argument("--tier",    type=int, default=None,
                        help="Override WORKER_TIER env var (1 or 2)")
    args = parser.parse_args()

    tier = args.tier or WORKER_TIER
    if tier not in (1, 2):
        print(f"ERROR: Invalid tier {tier}. Must be 1 or 2.")
        print("  (Claude escalation is now inline within tier-2 workers.)")
        sys.exit(1)

    project_root = (
        os.path.abspath(args.project) if args.project
        else os.getenv("PROJECT_DIR", os.getcwd())
    )

    if not GCP_PROJECT:
        print("ERROR: Set FIRESTORE_PROJECT_ID (or GCP_PROJECT) in .env or environment.")
        sys.exit(1)

    topic_name = TOPIC_MAP[tier]
    sub_name   = SUB_MAP[tier]

    print(f"Worker  : tier {tier}  ({topic_name})")
    print(f"Project : {project_root}")
    print(f"GCP     : {GCP_PROJECT}")
    if tier == 2:
        print("          (32B → Claude inline escalation enabled)")

    # Ensure subscription exists
    ensure_subscription(GCP_PROJECT, topic_name, sub_name)

    # Pull one task
    print(f"\n→ Pulling from {sub_name} (timeout {PULL_TIMEOUT}s)...")
    payload, handle = pull_one(GCP_PROJECT, sub_name, PULL_TIMEOUT)

    if payload is None:
        print("  No tasks available — exiting.")
        sys.exit(0)

    task_id   = payload["task_id"]
    task_desc = payload["description"]
    files     = payload.get("files", [])
    run_id    = payload.get("run_id", "unknown")

    print(f"\n  Task [{task_id}]: {task_desc[:80]}")
    print(f"  Files : {files}")

    start_time = time.time()
    try:
        if tier == 1:
            success, error = run_tier1(task_desc, project_root, files)
        else:
            success, error = run_tier2(task_desc, project_root, files)
    except Exception as exc:
        success = False
        error   = str(exc)
        print(f"  ⚠  Exception: {exc}")

    duration_s = time.time() - start_time
    status     = "done" if success else "failed"

    print(f"\n  Result : {status} in {duration_s:.1f}s")

    # Ack the Pub/Sub message first
    ack(handle)

    # Write result directly to Firestore (reliable; no Pub/Sub round-trip)
    write_result(GCP_PROJECT, run_id, task_id, status, tier, duration_s, error)


if __name__ == "__main__":
    main()
