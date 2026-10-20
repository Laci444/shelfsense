"""Build all derived stores (Neo4j, Qdrant) from the sources of record.

Source of record: PostgreSQL (members, loans) and MongoDB (book records, incl. descriptions).
Redis holds only expiring state (decisions, lists), so it is flushed here, not bulk-loaded.
Usage: python -m shelfsense.sync
"""
import time

from shelfsense import embeddings
from shelfsense.stores import mongo, neo4j_graph, postgres, qdrant_store, redis_cache


def sync_graph():
    """Rebuild only Neo4j (fast, no embeddings) from PostgreSQL + MongoDB."""
    books = list(mongo.connect()["books"].find({}))
    for b in books:
        b["id"] = b.pop("_id")
    with postgres.connect() as conn:
        members = [{"id": r[0]} for r in conn.execute("SELECT id FROM members")]
        loans = [{"member_id": r[0], "book_id": r[1]}
                 for r in conn.execute("SELECT member_id, book_id FROM loans")]
    driver = neo4j_graph.connect()
    neo4j_graph.rebuild(driver, books, members, loans)
    driver.close()


def sync_all():
    t0 = time.time()
    books = list(mongo.connect()["books"].find({}))
    for b in books:
        b["id"] = b.pop("_id")
    with postgres.connect() as conn:
        members = [{"id": r[0]} for r in conn.execute("SELECT id FROM members")]
        loans = [{"member_id": r[0], "book_id": r[1]}
                 for r in conn.execute("SELECT member_id, book_id FROM loans")]

    driver = neo4j_graph.connect()
    neo4j_graph.rebuild(driver, books, members, loans)
    driver.close()

    vectors = embeddings.embed([embeddings.book_text(b) for b in books])
    qdrant_store.rebuild(qdrant_store.connect(), books, vectors)

    redis_cache.connect().flushdb()
    return {"books": len(books), "members": len(members), "loans": len(loans),
            "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    print(sync_all())
