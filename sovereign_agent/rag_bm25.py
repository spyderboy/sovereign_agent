"""Sparse (BM25) lexical retrieval over the same chunk store rag_index builds.

Kept as an in-memory index cached per project root, rebuilt only when the
indexed file set changes (tracked via rag_index.files_fingerprint). Always
queried scoped to a task's candidate file set — never a bare full-project
query — since retrieval is always run against the small set of files
find_relevant_files() already chose.
"""

from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

import rag_chunking
import rag_index

_SPLIT_RE = re.compile(r"[^A-Za-z0-9_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_CACHE: dict[str, tuple[str, BM25Okapi, list[dict]]] = {}  # project_root -> (fingerprint, index, rows)


def tokenize_code(text: str) -> list[str]:
    """Split on non-identifier chars, then further split camelCase/snake_case
    (keeping both the compound token and its parts) for better partial-match
    recall on source code identifiers."""
    tokens: list[str] = []
    for raw in _SPLIT_RE.split(text):
        if not raw:
            continue
        tokens.append(raw.lower())
        for camel_part in _CAMEL_RE.split(raw):
            tokens.extend(p.lower() for p in camel_part.split("_") if p)
    return tokens


def _get_index(project_root: str) -> tuple[BM25Okapi, list[dict]] | tuple[None, None]:
    fp = rag_index.files_fingerprint(project_root)
    cached = _CACHE.get(project_root)
    if cached and cached[0] == fp:
        return cached[1], cached[2]
    rows = rag_index.all_chunk_rows(project_root)
    if not rows:
        _CACHE[project_root] = (fp, None, [])
        return None, []
    bm25 = BM25Okapi([tokenize_code(r["text"]) for r in rows])
    _CACHE[project_root] = (fp, bm25, rows)
    return bm25, rows


def sparse_search(project_root: str, query: str, limit: int,
                   file_filter: list[str] | None = None) -> list[rag_chunking.Chunk]:
    bm25, rows = _get_index(project_root)
    if bm25 is None:
        return []
    scores = bm25.get_scores(tokenize_code(query))
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    out: list[rag_chunking.Chunk] = []
    file_set = set(file_filter) if file_filter else None
    for i in ranked:
        if scores[i] <= 0:
            break
        row = rows[i]
        if file_set is not None and row["file_path"] not in file_set:
            continue
        out.append(rag_chunking.Chunk(
            id=row["id"], file_path=row["file_path"], symbol=row["symbol"],
            kind=row["kind"], start_line=row["start_line"], end_line=row["end_line"],
            text=row["text"],
        ))
        if len(out) >= limit:
            break
    return out
