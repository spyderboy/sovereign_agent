"""Cross-encoder reranking for the RAG subsystem.

Model choice: BAAI/bge-reranker-base (~278M params) — the architecture doc's
original suggestion. A smaller cross-encoder/ms-marco-MiniLM-L-6-v2 (~22M)
was tried first on the reasoning that reranking runs on every attempt of
every task and speed would matter most, but empirically it produced flat,
near-random scores on raw source-code text regardless of input framing
(MS-MARCO's training data is natural-language passages, not code) — e.g.
scoring a `void doNothing()` stub ABOVE a `buildRentChart(...)` method for
the query "Add a rent collected over time chart". bge-reranker-base
correctly ranked the chart-building method highest in the same test, and
its measured latency (~18ms/candidate, ~0.7s for a 40-candidate shortlist)
is negligible next to work.py's multi-minute per-task LLM calls — so there
was no real speed/accuracy tradeoff to make once actually measured, and
correctness wins outright. A reranker that can't discriminate relevance is
worse than no reranker at all, since it actively reorders a decent hybrid
shortlist toward noise.
"""

from __future__ import annotations

import os

import rag_chunking

RERANK_MODEL = os.getenv("RAG_RERANK_MODEL", "BAAI/bge-reranker-base")

_CROSS_ENCODER = None


def _get_cross_encoder():
    global _CROSS_ENCODER
    if _CROSS_ENCODER is None:
        from sentence_transformers import CrossEncoder
        _CROSS_ENCODER = CrossEncoder(RERANK_MODEL, max_length=512)
    return _CROSS_ENCODER


def rerank_with_scores(query: str, chunks: list[rag_chunking.Chunk]) -> list[tuple[rag_chunking.Chunk, float]]:
    """Score every candidate, sorted best-first. No top_k cutoff — callers that
    want diversity across files (see rag_retrieve) need the full scored pool."""
    if not chunks:
        return []
    ce = _get_cross_encoder()
    scores = ce.predict([(query, c.text) for c in chunks])
    return sorted(zip(chunks, scores), key=lambda pair: pair[1], reverse=True)


def rerank(query: str, chunks: list[rag_chunking.Chunk], top_k: int) -> list[rag_chunking.Chunk]:
    if not chunks:
        return []
    if len(chunks) <= top_k:
        return list(chunks)
    return [c for c, _ in rerank_with_scores(query, chunks)[:top_k]]
