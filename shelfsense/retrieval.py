"""Retrieval modes for free-text search: vector (Qdrant), keyword (PostgreSQL full text) and hybrid.

Hybrid = reciprocal rank fusion (RRF) of the two ranked lists: score(id) = sum over lists of 1 / (K + rank).
RRF needs no score calibration (cosine similarity and ts_rank are on different scales), only the ranks.
"""
import time

from shelfsense import embeddings
from shelfsense.stores import fts_store, qdrant_store

MODES = ("vector", "keyword", "hybrid")
K = 60            # standard RRF constant
POOL = 50         # candidates taken from each list before fusing


def rrf(*ranked, k=K):
    """Fuse ranked lists of (id, score) into one list of (id, rrf_score), best first."""
    score = {}
    for lst in ranked:
        for rank, (i, _) in enumerate(lst, start=1):
            score[i] = score.get(i, 0.0) + 1.0 / (k + rank)
    return sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))


def search(qdrant, pg, text, mode="hybrid", limit=5, genre=None, vector=None):
    """[(book_id, score)]. `vector` may be passed when the caller already embedded the text."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    pool = max(limit, POOL)
    vec_hits = kw_hits = []
    if mode in ("vector", "hybrid"):
        vec = embeddings.embed([text])[0] if vector is None else vector
        vec_hits = qdrant_store.search(qdrant, vec, limit=pool, genre=genre, hnsw_ef=128)
    if mode in ("keyword", "hybrid"):
        kw_hits = fts_store.search(pg, text, limit=pool, genre=genre)
    if mode == "vector":
        return vec_hits[:limit]
    if mode == "keyword":
        return kw_hits[:limit]
    return rrf(vec_hits, kw_hits)[:limit]


def timed_search(qdrant, pg, text, **kw):
    t = time.perf_counter()
    res = search(qdrant, pg, text, **kw)
    return res, (time.perf_counter() - t) * 1000
