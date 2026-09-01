"""Historical failure-memory retrieval for the RAG subsystem.

Mines logs/task_traces.jsonl — which work.py's _log_attempt_trace() already
writes on every attempt, with no changes needed to the retry loop itself —
for (error_text -> successful fix) pairs: records where record_type is
"attempt", validation_passed is True, and errors_fed_in is non-empty (i.e.
this attempt was given a prior failure and went on to pass). Those pairs
are embedded and indexed so a future task hitting a similar error can be
shown the proven fix pattern.

Incremental via a byte-offset marker file, so re-running mid-session on a
multi-megabyte trace log never re-scans from the start.
"""

from __future__ import annotations

import hashlib
import json
import os

import rag_index
import rag_ollama_embed

MIN_ERROR_LEN = 20
FAILURE_MATCH_THRESHOLD = 0.35  # cosine distance (lower = more similar); tune during rollout


def _offset_path(project_root: str) -> str:
    return os.path.join(project_root, ".sovereign_rag", "mine_offset.txt")


def mine_failure_fixes(project_root: str, min_error_len: int = MIN_ERROR_LEN) -> int:
    """Incrementally mine task_traces.jsonl for new failure->fix pairs. Returns count added."""
    trace_path = os.path.join(project_root, "logs", "task_traces.jsonl")
    if not os.path.exists(trace_path):
        return 0

    offset_path = _offset_path(project_root)
    start_offset = 0
    if os.path.exists(offset_path):
        try:
            start_offset = int(open(offset_path).read().strip())
        except (ValueError, OSError):
            start_offset = 0

    new_rows = []
    with open(trace_path, encoding="utf8", errors="replace") as f:
        f.seek(start_offset)
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("record_type") != "attempt" or not rec.get("validation_passed"):
                continue
            err = (rec.get("errors_fed_in") or "").strip()
            fix = (rec.get("raw_output") or "").strip()
            if len(err) < min_error_len or not fix:
                continue
            new_rows.append({
                "id": hashlib.sha1(err[:800].encode("utf8")).hexdigest()[:16],
                "task_idx": rec.get("task_idx", -1),
                "attempt": rec.get("attempt", -1),
                "error_text": err[:1500],
                "fix_text": fix[:4000],
                "files_written": ",".join(rec.get("files_written") or []),
                "date": rec.get("date", ""),
            })
        end_offset = f.tell()

    os.makedirs(os.path.dirname(offset_path), exist_ok=True)
    open(offset_path, "w").write(str(end_offset))

    if not new_rows:
        return 0

    db = rag_index._db(project_root)
    rag_index._ensure_tables(db)
    tbl = db.open_table("_failure_fixes")
    existing_ids = {r["id"] for r in tbl.to_arrow().to_pylist()} if tbl.count_rows() else set()
    fresh = [r for r in new_rows if r["id"] not in existing_ids]
    if not fresh:
        return 0

    vectors = rag_ollama_embed.embed_batch([f"search_document: {r['error_text']}" for r in fresh])
    for row, v in zip(fresh, vectors):
        row["vector"] = v
    tbl.add(fresh)
    return len(fresh)


def retrieve_similar_failure_fix(project_root: str, current_errors: str,
                                  threshold: float = FAILURE_MATCH_THRESHOLD) -> str | None:
    if not current_errors or not current_errors.strip():
        return None
    db = rag_index._db(project_root)
    if "_failure_fixes" not in db.table_names():
        return None
    tbl = db.open_table("_failure_fixes")
    if tbl.count_rows() == 0:
        return None

    qvec = rag_ollama_embed.embed_one(f"search_query: {current_errors[:1500]}")
    hits = tbl.search(qvec).metric("cosine").limit(1).to_list()
    if not hits or hits[0].get("_distance", 1.0) > threshold:
        return None

    h = hits[0]
    return (
        f"\n\nHISTORICAL FIX (a similar error was resolved before in this project — "
        f"task #{h['task_idx']} attempt {h['attempt']}):\n"
        f"Prior error:\n{h['error_text'][:800]}\n\n"
        f"Successful fix applied:\n{h['fix_text'][:2000]}\n"
        f"(Adapt this to the CURRENT file/error — do not copy verbatim if context differs.)"
    )
