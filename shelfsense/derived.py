"""Registry of derived data (table derived_state).

For every (store, book) it records the books.version the store last absorbed and when. A book is
  current  - registry version == books.version
  stale    - registry version differs (the source changed after the last refresh)
  missing  - no registry row (never indexed, or the update was not delivered)
and a registry row without a book is an orphan (the book was deleted at the source).
Stores: neo4j (book node), qdrant (point), pgvector (vector row), fts (tsvector row).
"""
STORES = ("neo4j", "qdrant", "pgvector", "fts")


def mark(conn, store, book_id):
    """Record that `store` now reflects the current version of the book (read from PostgreSQL)."""
    conn.execute("INSERT INTO derived_state (store, source_id, source_version) "
                 "SELECT %s, id, version FROM books WHERE id = %s "
                 "ON CONFLICT (store, source_id) DO UPDATE SET source_version = EXCLUDED.source_version, "
                 "refreshed_at = now()", (store, book_id))


def touch_if_tracked(conn, store, book_id):
    """The change did not affect this store's content (e.g. a genre edit for pgvector, which filters live)."""
    conn.execute("UPDATE derived_state d SET source_version = b.version, refreshed_at = now() FROM books b "
                 "WHERE d.store = %s AND d.source_id = %s AND b.id = d.source_id", (store, book_id))


def forget(conn, store, book_id):
    conn.execute("DELETE FROM derived_state WHERE store = %s AND source_id = %s", (store, book_id))


def adopt(conn, stores=STORES):
    """Declare every book current in the given stores. Only for stores that were just built from the
    sources (e.g. right after `db sync`) while their registry rows are missing."""
    for s in stores:
        conn.execute("INSERT INTO derived_state (store, source_id, source_version) SELECT %s, id, version FROM books "
                     "ON CONFLICT (store, source_id) DO UPDATE SET source_version = EXCLUDED.source_version, "
                     "refreshed_at = now()", (s,))
        conn.execute("DELETE FROM derived_state d WHERE d.store = %s AND NOT EXISTS "
                     "(SELECT 1 FROM books b WHERE b.id = d.source_id)", (s,))
    if not conn.autocommit:
        conn.commit()


def status(conn):
    """{store: {current, stale, missing, orphans, last_refresh}}"""
    out = {}
    for s in STORES:
        cur, stale, missing, last = conn.execute(
            "SELECT count(*) FILTER (WHERE d.source_version = b.version), "
            "       count(*) FILTER (WHERE d.source_id IS NOT NULL AND d.source_version <> b.version), "
            "       count(*) FILTER (WHERE d.source_id IS NULL), max(d.refreshed_at) "
            "FROM books b LEFT JOIN derived_state d ON d.store = %s AND d.source_id = b.id", (s,)).fetchone()
        orphans = conn.execute("SELECT count(*) FROM derived_state d WHERE d.store = %s AND NOT EXISTS "
                               "(SELECT 1 FROM books b WHERE b.id = d.source_id)", (s,)).fetchone()[0]
        out[s] = {"current": cur, "stale": stale, "missing": missing, "orphans": orphans, "last_refresh": last}
    return out


def pending(conn, store):
    """Book ids that are stale or missing in `store`."""
    return [r[0] for r in conn.execute(
        "SELECT b.id FROM books b LEFT JOIN derived_state d ON d.store = %s AND d.source_id = b.id "
        "WHERE d.source_id IS NULL OR d.source_version <> b.version ORDER BY b.id", (store,))]


def orphans(conn, store):
    return [r[0] for r in conn.execute(
        "SELECT d.source_id FROM derived_state d WHERE d.store = %s AND NOT EXISTS "
        "(SELECT 1 FROM books b WHERE b.id = d.source_id)", (store,))]


def entry(conn, store, book_id):
    """(source_version, refreshed_at) or None."""
    return conn.execute("SELECT source_version, refreshed_at FROM derived_state WHERE store = %s AND source_id = %s",
                        (store, book_id)).fetchone()
