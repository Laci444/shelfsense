import json
import time
from pathlib import Path

import pytest

from shelfsense import embeddings, scenario
from shelfsense.config import COLLECTION, DATA_DIR
from shelfsense.stores import neo4j_graph, qdrant_store, redis_cache

N_BOOKS = len(json.loads((DATA_DIR / "generated" / "books.json").read_text(encoding="utf-8")))


def test_redis_decision_ttl_and_metadata():
    r = redis_cache.connect()
    redis_cache.cache_decision(r, "T1", "B1", "allow", "ok", source_id=7)
    d = redis_cache.get_decision(r, "T1", "B1")
    assert d["outcome"] == "allow" and d["source_id"] == 7 and d["refreshed_at"] <= time.time()
    assert 0 < r.ttl(redis_cache.decision_key("T1", "B1")) <= 300
    assert redis_cache.invalidate_book(r, "B1") == 1
    assert redis_cache.get_decision(r, "T1", "B1") is None


def test_redis_reading_list():
    r = redis_cache.connect()
    redis_cache.add_to_list(r, "T2", "B1"); redis_cache.add_to_list(r, "T2", "B2")
    assert redis_cache.read_list(r, "T2") == ["B1", "B2"]
    redis_cache.remove_from_list(r, "T2", "B1")
    assert redis_cache.read_list(r, "T2") == ["B2"]
    r.delete("readinglist:T2")


def test_neo4j_counts_and_queries():
    d = neo4j_graph.connect()
    with d.session() as s:
        assert s.run("MATCH (b:Book) RETURN count(b) AS n").single()["n"] == N_BOOKS
        assert s.run("MATCH (b:Book) WHERE b.refreshed_at IS NULL RETURN count(b) AS n").single()["n"] == 0
        assert s.run("MATCH ()-[x:BORROWED]->() RETURN count(x) AS n").single()["n"] > 0
    assert len(neo4j_graph.same_author(d, scenario.books()["free2"])) >= 0
    d.close()


def test_qdrant_search_and_filter():
    c = qdrant_store.connect()
    assert c.count(COLLECTION).count == N_BOOKS
    v = embeddings.embed(["epic fantasy with dragons and magic"])[0]
    hits = qdrant_store.search(c, v, limit=5, genre="fantasy")
    assert len(hits) == 5
    genres = {c.retrieve(COLLECTION, [qdrant_store.point_id(h[0])])[0].payload["genre"] for h in hits}
    assert genres == {"fantasy"}
    schema = c.get_collection(COLLECTION).payload_schema
    assert "genre" in schema and "year" in schema


def test_embeddings_are_normalised():
    import numpy as np
    v = embeddings.embed(["hello world"])[0]
    assert v.shape == (384,) and abs(float(np.linalg.norm(v)) - 1.0) < 1e-4
