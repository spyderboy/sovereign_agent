"""Ollama embeddings client for the RAG subsystem.

Uses the batch /api/embed endpoint (confirmed supported by the local Ollama
install, v0.32.13) rather than the legacy single-prompt /api/embeddings, so
indexing many short chunk texts amortizes HTTP round-trip overhead.

nomic-embed-text is trained with asymmetric task prefixes — callers must
prepend "search_document: " when embedding text going INTO the index, and
"search_query: " when embedding a query used to search it. This module does
not add those prefixes itself (the caller knows which side it's on); see
rag_index.sync_index() and rag_retrieve.retrieve_context_chunks().
"""

from __future__ import annotations

import os

import requests

from sov.config import OLLAMA_URL

EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "nomic-embed-text")
DEFAULT_BATCH_SIZE = 32


def embed_batch(texts: list[str], model: str = EMBED_MODEL,
                 batch_size: int = DEFAULT_BATCH_SIZE, timeout: int = 120) -> list[list[float]]:
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": model, "input": batch},
            timeout=timeout,
        )
        resp.raise_for_status()
        out.extend(resp.json()["embeddings"])
    return out


def embed_one(text: str, model: str = EMBED_MODEL, timeout: int = 60) -> list[float]:
    return embed_batch([text], model=model, timeout=timeout)[0]
