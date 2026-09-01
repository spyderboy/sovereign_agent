"""Per-project LanceDB vector index for the RAG subsystem.

Storage lives at <project_root>/.sovereign_rag/ — gitignored the same way
logs/ already is for target projects (a bare line in the project's own
.gitignore).

Re-indexing strategy is delete-by-file_path then add, not a merge/upsert
API — simpler and unambiguous at the scale this runs at (tens of changed
files per sync, not thousands), and avoids depending on LanceDB's
merge_insert kwarg surface, which has churned across versions.
"""

from __future__ import annotations

import hashlib
import os

import lancedb
import pyarrow as pa

import rag_chunking
import rag_ollama_embed

VECTOR_DIM = 768  # nomic-embed-text output dimension


def _rag_dir(project_root: str) -> str:
    return os.path.join(project_root, ".sovereign_rag")


def _db(project_root: str):
    return lancedb.connect(_rag_dir(project_root))


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def _ensure_tables(db) -> None:
    existing = set(db.table_names())
    if "chunks" not in existing:
        db.create_table("chunks", schema=pa.schema([
            pa.field("id", pa.string()),
            pa.field("file_path", pa.string()),
            pa.field("symbol", pa.string()),
            pa.field("kind", pa.string()),
            pa.field("start_line", pa.int32()),
            pa.field("end_line", pa.int32()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
        ]))
    if "_files" not in existing:
        db.create_table("_files", schema=pa.schema([
            pa.field("file_path", pa.string()),
            pa.field("mtime", pa.float64()),
            pa.field("content_hash", pa.string()),
            pa.field("chunk_count", pa.int32()),
        ]))
    if "_failure_fixes" not in existing:
        db.create_table("_failure_fixes", schema=pa.schema([
            pa.field("id", pa.string()),
            pa.field("task_idx", pa.int32()),
            pa.field("attempt", pa.int32()),
            pa.field("error_text", pa.string()),
            pa.field("fix_text", pa.string()),
            pa.field("files_written", pa.string()),
            pa.field("date", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
        ]))


def _row_to_chunk(row: dict) -> rag_chunking.Chunk:
    return rag_chunking.Chunk(
        id=row["id"], file_path=row["file_path"], symbol=row["symbol"],
        kind=row["kind"], start_line=row["start_line"], end_line=row["end_line"],
        text=row["text"],
    )


def sync_index(project_root: str, candidate_files: list[str] | None = None) -> int:
    """Incrementally re-chunk+re-embed changed files. Returns count re-indexed."""
    db = _db(project_root)
    _ensure_tables(db)
    files_tbl = db.open_table("_files")
    chunks_tbl = db.open_table("chunks")

    manifest_rows = files_tbl.to_arrow().to_pylist() if files_tbl.count_rows() else []
    manifest = {r["file_path"]: r for r in manifest_rows}

    if candidate_files is not None:
        targets = candidate_files
    else:
        from work import all_source_files  # lazy import — avoid a cycle at module load time
        targets = all_source_files(project_root)

    reindexed = 0
    for rel in targets:
        full = os.path.join(project_root, rel)
        if not os.path.exists(full):
            continue
        st = os.stat(full)
        prior = manifest.get(rel)
        if prior and prior["mtime"] == st.st_mtime:
            continue  # fast path — untouched since last sync
        content_hash = hashlib.sha256(open(full, "rb").read()).hexdigest()
        if prior and prior["content_hash"] == content_hash:
            # mtime changed (e.g. a checkout) but content is identical — just
            # refresh the manifest row, no re-chunk/re-embed needed.
            files_tbl.delete(f"file_path = '{_sql_escape(rel)}'")
            files_tbl.add([{"file_path": rel, "mtime": st.st_mtime,
                             "content_hash": content_hash, "chunk_count": prior["chunk_count"]}])
            continue

        chunks = rag_chunking.chunk_file(rel, project_root)
        chunks_tbl.delete(f"file_path = '{_sql_escape(rel)}'")
        if chunks:
            vectors = rag_ollama_embed.embed_batch([f"search_document: {c.text}" for c in chunks])
            rows = []
            for c, v in zip(chunks, vectors):
                rows.append({"id": c.id, "file_path": c.file_path, "symbol": c.symbol,
                             "kind": c.kind, "start_line": c.start_line, "end_line": c.end_line,
                             "text": c.text, "vector": v})
            chunks_tbl.add(rows)

        files_tbl.delete(f"file_path = '{_sql_escape(rel)}'")
        files_tbl.add([{"file_path": rel, "mtime": st.st_mtime,
                         "content_hash": content_hash, "chunk_count": len(chunks)}])
        reindexed += 1

    return reindexed


def dense_search(project_root: str, query_vector: list[float], limit: int,
                  file_filter: list[str] | None = None) -> list[rag_chunking.Chunk]:
    db = _db(project_root)
    if "chunks" not in db.table_names():
        return []
    tbl = db.open_table("chunks")
    if tbl.count_rows() == 0:
        return []
    q = tbl.search(query_vector).metric("cosine")
    if file_filter:
        clause = ",".join(f"'{_sql_escape(f)}'" for f in file_filter)
        q = q.where(f"file_path IN ({clause})")
    return [_row_to_chunk(r) for r in q.limit(limit).to_list()]


def all_chunk_rows(project_root: str) -> list[dict]:
    """Full chunk-table contents, for BM25's in-memory index to build from."""
    db = _db(project_root)
    if "chunks" not in db.table_names():
        return []
    tbl = db.open_table("chunks")
    return tbl.to_arrow().to_pylist() if tbl.count_rows() else []


def files_fingerprint(project_root: str) -> str:
    """Cheap fingerprint of indexed-file state, for BM25 cache invalidation."""
    db = _db(project_root)
    if "_files" not in db.table_names():
        return ""
    rows = db.open_table("_files").to_arrow().to_pylist()
    key = sorted((r["file_path"], r["content_hash"]) for r in rows)
    return hashlib.sha1(repr(key).encode("utf8")).hexdigest()
