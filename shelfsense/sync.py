"""Build all derived stores (Neo4j, Qdrant, pgvector, full text) from the sources of record.

Source of record: PostgreSQL (members, loans) and MongoDB (book records, incl. descriptions).
Redis holds only expiring state (decisions, lists), so it is flushed here, not bulk-loaded.
Every build also writes the derived_state registry (which book version each store absorbed).
Usage: python -m shelfsense.sync
"""
import time

from shelfsense import derived, embeddings
from shelfsense.stores import fts_store, mongo, neo4j_graph, pgvector_store, postgres, qdrant_store, redis_cache


def _books(conn):
    """Mongo book documents, each with the PostgreSQL version of that book."""
    books = list(mongo.connect()["books"].find({}))
    versions = dict(conn.execute("SELECT id, version FROM books").fetchall())
    for b in books:
        b["id"] = b.pop("_id")
        b["version"] = versions.get(b["id"])
    return books


def _members_loans(conn):
    return ([{"id": r[0]} for r in conn.execute("SELECT id FROM members")],
            [{"member_id": r[0], "book_id": r[1]} for r in conn.execute("SELECT member_id, book_id FROM loans")])


def sync_graph():
    """Rebuild only Neo4j (fast, no embeddings) from PostgreSQL + MongoDB."""
    with postgres.connect() as conn:
        books = _books(conn)
        members, loans = _members_loans(conn)
        driver = neo4j_graph.connect()
        neo4j_graph.rebuild(driver, books, members, loans)
        driver.close()
        derived.adopt(conn, ["neo4j"])


def sync_fts():
    """Rebuild only the full-text table from MongoDB."""
    with postgres.connect() as conn:
        books = _books(conn)
        out = fts_store.rebuild(conn, [b["id"] for b in books], [embeddings.book_text(b) for b in books])
        derived.adopt(conn, ["fts"])
        return out


def sync_pgvector_from_qdrant():
    """Fill pgvector with the vectors Qdrant already holds (no re-embedding): same vectors in both stores."""
    q = qdrant_store.connect()
    ids, vecs, offset = [], [], None
    while True:
        pts, offset = q.scroll(qdrant_store.COLLECTION, limit=1000, offset=offset, with_payload=["source_id"],
                               with_vectors=True)
        for p in pts:
            ids.append(p.payload["source_id"]); vecs.append(p.vector)
        if offset is None:
            break
    with postgres.connect() as conn:
        out = pgvector_store.rebuild(conn, ids, vecs)
        derived.adopt(conn, ["pgvector"])
        return out


def sync_all():
    t0 = time.time()
    with postgres.connect() as conn:
        books = _books(conn)
        members, loans = _members_loans(conn)

    driver = neo4j_graph.connect()
    neo4j_graph.rebuild(driver, books, members, loans)
    driver.close()

    texts = [embeddings.book_text(b) for b in books]
    vectors = embeddings.embed(texts)
    qdrant_store.rebuild(qdrant_store.connect(), books, vectors)
    with postgres.connect() as conn:
        ids = [b["id"] for b in books]
        pgvector_store.rebuild(conn, ids, vectors)
        fts_store.rebuild(conn, ids, texts)
        derived.adopt(conn)

    redis_cache.connect().flushdb()
    return {"books": len(books), "members": len(members), "loans": len(loans),
            "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    print(sync_all())
