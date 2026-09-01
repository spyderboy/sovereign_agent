"""Hybrid retrieval orchestration for the RAG subsystem.

Flow: embed the task text -> dense search + sparse (BM25) search, both
scoped to the task's candidate file set -> merge/dedupe by chunk id ->
cross-encoder rerank down to top_k -> group by file for prompt injection.
"""

from __future__ import annotations

import os

import rag_bm25
import rag_chunking
import rag_index
import rag_ollama_embed
import rag_rerank

# The binding constraint — see _select_diverse's docstring for why this is a
# char budget, not a chunk count. 6000 is a starting default (~2000 tokens);
# tunable per-project via env if file sizes or a model's context window
# warrant a different value.
DEFAULT_MAX_TOTAL_CHARS = int(os.getenv("RAG_MAX_TOTAL_CHARS", "6000"))
DEFAULT_MAX_CHUNKS = int(os.getenv("RAG_MAX_CHUNKS", "20"))  # loose secondary cap


def _select_diverse(scored: list[tuple[rag_chunking.Chunk, float]],
                     max_total_chars: int, max_chunks: int) -> list[rag_chunking.Chunk]:
    """Greedily fill a CHARACTER budget (not a chunk count) favoring file
    coverage: every file with at least one candidate gets a shot at its best
    remaining chunk, round-robin, before any file gets a second.

    Why a char budget and not top_k: measured directly during rollout
    validation on EstateWiseFlutter's actual codebase (small, simple Dart
    files) — capping by chunk COUNT does not reliably bound total prompt
    size. When most candidate files are already small, "keep each file's
    best chunk" is often nearly the whole file, so a fixed top_k (e.g. 12
    chunks across 10 files) reproduced ~the same total size as whole-file
    reading, plus per-chunk label overhead actually made it slightly larger.
    The real lever that prevents PromptTooLargeError is a hard cap on total
    injected characters, independent of how many files/chunks exist.

    Diversity is still worth keeping even under a char budget: it's what
    stopped one broadly-relevant file's several methods from silently
    consuming the whole budget while other genuinely relevant files got
    nothing (also observed directly — a 5-method repository crowded out two
    other relevant files entirely under an earlier count-only design).
    """
    by_file: dict[str, list[tuple[rag_chunking.Chunk, float]]] = {}
    for c, score in scored:
        by_file.setdefault(c.file_path, []).append((c, score))
    for pairs in by_file.values():
        pairs.sort(key=lambda p: p[1], reverse=True)

    selected: list[rag_chunking.Chunk] = []
    selected_ids: set[str] = set()
    total_chars = 0

    round_idx = 0
    while len(selected) < max_chunks and total_chars < max_total_chars:
        candidates_this_round = []
        for file_path, pairs in by_file.items():
            if round_idx < len(pairs):
                candidates_this_round.append(pairs[round_idx])
        if not candidates_this_round:
            break
        candidates_this_round.sort(key=lambda pair: pair[1], reverse=True)
        made_progress = False
        for c, _score in candidates_this_round:
            if len(selected) >= max_chunks or total_chars >= max_total_chars:
                break
            if c.id in selected_ids:
                continue
            selected.append(c)
            selected_ids.add(c.id)
            total_chars += len(c.text)
            made_progress = True
        round_idx += 1
        if not made_progress:
            break

    return selected


def retrieve_context_chunks(task: str, query_files: list[str], project_root: str,
                             max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
                             max_chunks: int = DEFAULT_MAX_CHUNKS,
                             dense_n: int = 30, sparse_n: int = 30
                             ) -> dict[str, list[rag_chunking.Chunk]]:
    """query_files: the candidate SUPPORT files (caller excludes the task's
    own target file — that one stays whole-file, per work.py integration).
    max_total_chars is the binding constraint on injected prompt size (see
    _select_diverse); max_chunks is a loose secondary cap. Returns
    {file_path: [Chunk, ...]}, chunks sorted by start_line within each file.
    """
    if not query_files:
        return {}

    query_vec = rag_ollama_embed.embed_one(f"search_query: {task}")
    dense_hits = rag_index.dense_search(project_root, query_vec, dense_n, file_filter=query_files)
    sparse_hits = rag_bm25.sparse_search(project_root, task, sparse_n, file_filter=query_files)

    merged: dict[str, rag_chunking.Chunk] = {}
    for c in dense_hits + sparse_hits:
        merged.setdefault(c.id, c)
    if not merged:
        return {}

    candidates = list(merged.values())
    scored = rag_rerank.rerank_with_scores(task, candidates)
    selected = _select_diverse(scored, max_total_chars, max_chunks)

    grouped: dict[str, list[rag_chunking.Chunk]] = {}
    for c in selected:
        grouped.setdefault(c.file_path, []).append(c)
    for chunks in grouped.values():
        chunks.sort(key=lambda c: c.start_line)
    return grouped


def format_chunks_for_prompt(grouped: dict[str, list[rag_chunking.Chunk]]) -> str:
    blocks = []
    for file_path, chunks in grouped.items():
        parts = [f"=== {file_path} (relevant excerpts — not the full file) ==="]
        for c in chunks:
            label = f"{c.kind} {c.symbol}" if c.symbol else c.kind
            parts.append(f"--- {label} (lines {c.start_line}-{c.end_line}) ---\n{c.text}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)
