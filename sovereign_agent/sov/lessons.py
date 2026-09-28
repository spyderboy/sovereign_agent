"""Firestore sync of learned lessons (error patterns, rule drafts, velocity).

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from datetime import date
import json
import os
from sov import state
from sov.telemetry import VELOCITY_LOG
from sov.ui import DIM, RESET, YELLOW


# ─── Firestore bidirectional sync ─────────────────────────────────────────────

def _firestore_client():
    """Return a Firestore Client, or None if unavailable/unconfigured."""
    if not FIRESTORE_PROJECT:
        return None
    try:
        from google.cloud import firestore as _fs  # type: ignore
        return _fs.Client(project=FIRESTORE_PROJECT)
    except ImportError:
        print(f"  {YELLOW}google-cloud-firestore not installed — "
              f"pip install google-cloud-firestore{RESET}")
        return None
    except Exception as e:
        print(f"  {YELLOW}Firestore init failed: {e}{RESET}")
        return None


def _firestore_push_lessons(project_root: str) -> None:
    """Upload local error patterns, rule drafts, and velocity to Firestore.

    Uses merge=True so concurrent cloud workers never clobber each other.
    Collections written:
      error_patterns  — keyed by error code
      project_rules   — keyed by MD5 of rule text
      task_velocity   — keyed by MD5 of date+task (last 50 records)
    """
    db = _firestore_client()
    if db is None:
        return

    import hashlib as _hl2
    pushed = 0

    try:
        # ── 1. Error patterns ─────────────────────────────────────────────────
        errors_path = os.path.join(project_root, "logs", "errors.jsonl")
        if os.path.exists(errors_path):
            col = db.collection("error_patterns")
            with open(errors_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        code = (rec.get("code") or rec.get("error_code")
                                or rec.get("pattern", ""))
                        if not code:
                            continue
                        doc_id = f"{state.PROJECT_ID}_{code}".replace("/", "_")[:128]
                        col.document(doc_id).set({
                            "code":      code,
                            "count":     rec.get("count", 1),
                            "last_task": rec.get("task", "")[:120],
                            "last_date": rec.get("date", date.today().isoformat()),
                            "hint":      rec.get("hint", ""),
                            "source":    "local",
                            "project":   state.PROJECT_ID,
                        }, merge=True)
                        pushed += 1
                    except Exception:
                        pass

        # ── 2. Rule drafts ────────────────────────────────────────────────────
        rules_path = os.path.join(project_root, "logs", "rule_drafts.jsonl")
        if os.path.exists(rules_path):
            col = db.collection("project_rules")
            with open(rules_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        rule_text = rec.get("rule") or rec.get("new_rule", "")
                        if not rule_text:
                            continue
                        doc_id = f"{state.PROJECT_ID}_" + _hl2.md5(rule_text.encode()).hexdigest()[:16]
                        col.document(doc_id).set({
                            "rule":      rule_text,
                            "task_idx":  rec.get("task_idx", 0),
                            "date":      rec.get("date", date.today().isoformat()),
                            "source":    "local",
                            "applied":   False,
                            "project":   state.PROJECT_ID,
                        }, merge=True)
                        pushed += 1
                    except Exception:
                        pass

        # ── 3. Velocity (last 50 records — analytics) ─────────────────────────
        vel_path = os.path.join(project_root, VELOCITY_LOG)
        if os.path.exists(vel_path):
            col = db.collection("task_velocity")
            with open(vel_path) as f:
                lines = [l for l in f if l.strip()]
            for line in lines[-50:]:
                try:
                    rec = json.loads(line)
                    key = rec.get("date", "") + rec.get("task", "")[:60]
                    doc_id = f"{state.PROJECT_ID}_" + _hl2.md5(key.encode()).hexdigest()[:16]
                    col.document(doc_id).set({**rec, "project": state.PROJECT_ID}, merge=True)
                    pushed += 1
                except Exception:
                    pass

        if pushed:
            print(f"  {DIM}☁  Pushed {pushed} lesson record(s) to Firestore{RESET}")

    except Exception as e:
        print(f"  {DIM}(Firestore push failed: {e}){RESET}")


def _firestore_pull_lessons(project_root: str) -> None:
    """Download remote error patterns and rule drafts from Firestore.

    Merges cloud-learned patterns into local log files so this worker benefits
    from everything other workers (local or cloud) have already discovered.
    Safe to call repeatedly — skips records already present locally.
    """
    db = _firestore_client()
    if db is None:
        return

    print(f"  {DIM}↕  Pulling lessons from Firestore ({FIRESTORE_PROJECT})...{RESET}",
          end="", flush=True)
    pulled = 0

    try:
        # ── 1. Error patterns ─────────────────────────────────────────────────
        errors_path = os.path.join(project_root, "logs", "errors.jsonl")
        os.makedirs(os.path.dirname(errors_path), exist_ok=True)
        existing_codes: set[str] = set()
        if os.path.exists(errors_path):
            with open(errors_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        code = (rec.get("code") or rec.get("error_code")
                                or rec.get("pattern", ""))
                        if code:
                            existing_codes.add(code)
                    except Exception:
                        pass

        new_patterns: list[str] = []
        for doc in db.collection("error_patterns").where("project", "==", state.PROJECT_ID).stream():
            d = doc.to_dict()
            code = d.get("code", "")
            if code and code not in existing_codes:
                new_patterns.append(json.dumps(d))
                existing_codes.add(code)
                pulled += 1

        if new_patterns:
            with open(errors_path, "a") as f:
                for line in new_patterns:
                    f.write(line + "\n")

        # ── 2. Rule drafts ────────────────────────────────────────────────────
        rules_path = os.path.join(project_root, "logs", "rule_drafts.jsonl")
        existing_rules: set[str] = set()
        if os.path.exists(rules_path):
            with open(rules_path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        rule = rec.get("rule") or rec.get("new_rule", "")
                        if rule:
                            existing_rules.add(rule[:80])
                    except Exception:
                        pass

        new_rules: list[str] = []
        for doc in db.collection("project_rules").where("project", "==", state.PROJECT_ID).stream():
            d = doc.to_dict()
            rule = d.get("rule", "")
            if rule and rule[:80] not in existing_rules:
                new_rules.append(json.dumps(d))
                existing_rules.add(rule[:80])
                pulled += 1

        if new_rules:
            with open(rules_path, "a") as f:
                for line in new_rules:
                    f.write(line + "\n")

        print(f" {DIM}{pulled} new record(s){RESET}")

    except Exception as e:
        print(f" {YELLOW}failed: {e}{RESET}")

# ── Firestore sync ─────────────────────────────────────────────────────────────
# Set FIRESTORE_PROJECT_ID in .env to enable bidirectional sync with cloud workers.
# Requires: pip install google-cloud-firestore
# Auth:      GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json  OR  gcloud auth
FIRESTORE_PROJECT = os.getenv("FIRESTORE_PROJECT_ID", "")
