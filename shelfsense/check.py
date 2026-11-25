"""Data validation across all stores (`shelfsense db check`).

Each check returns (name, ok, detail). Sources are checked against their own rules, the derived stores
against the sources. Nothing is modified.
"""
from collections import Counter

from shelfsense import derived, labels
from shelfsense.stores import fts_store, mongo, neo4j_graph, pgvector_store, postgres, qdrant_store


def _pg_checks(c):
    q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
    yield "postgres: available = total - active loans", *_zero(
        q("SELECT count(*) FROM books b WHERE b.available <> b.total - "
          "(SELECT count(*) FROM loans l WHERE l.book_id = b.id AND l.returned_at IS NULL)"), "books with a wrong count")
    yield "postgres: no duplicate active loan (member, book)", *_zero(
        q("SELECT count(*) FROM (SELECT 1 FROM loans WHERE returned_at IS NULL GROUP BY member_id, book_id "
          "HAVING count(*) > 1) x"), "duplicated pairs")
    yield "postgres: no member both holding and waiting for a book", *_zero(
        q("SELECT count(*) FROM waitlist w JOIN loans l ON l.member_id = w.member_id AND l.book_id = w.book_id "
          "AND l.returned_at IS NULL"), "conflicts")
    yield "postgres: loans due after they start", *_zero(
        q("SELECT count(*) FROM loans WHERE due_at <= loaned_at"), "bad loans")
    dup = q("SELECT count(*) FROM (SELECT 1 FROM books GROUP BY lower(title), year HAVING count(*) > 1) x")
    yield ("postgres: duplicate title+year (warning only)", True,
           f"warning: {dup} groups, distinct OpenLibrary works kept" if dup else "")
    yield ("postgres: exactly one active rule version",
           q("SELECT count(*) FROM rules WHERE valid_to IS NULL") == 1, "")
    yield "postgres: books without title / genre", *_zero(
        q("SELECT count(*) FROM books WHERE btrim(title) = '' OR btrim(genre) = ''"), "empty values")


def _zero(n, what):
    return n == 0, f"{n} {what}" if n else ""


def run():
    out = []
    add = lambda name, ok, detail="": out.append((name, bool(ok), detail))  # noqa: E731
    with postgres.connect() as c:
        c.autocommit = True
        for name, ok, detail in _pg_checks(c):
            add(name, ok, detail)
        pg_ids = {r[0] for r in c.execute("SELECT id FROM books")}
        n = len(pg_ids)

        coll = mongo.connect()["books"]
        docs = list(coll.find({}, {"_id": 1, "description": 1, "title": 1, "authors": 1}))
        m_ids = [d["_id"] for d in docs]
        dup = [i for i, k in Counter(m_ids).items() if k > 1]
        add("mongo: same book ids as PostgreSQL", set(m_ids) == pg_ids and not dup,
            f"only in mongo {len(set(m_ids) - pg_ids)}, only in postgres {len(pg_ids - set(m_ids))}")
        short = [d["_id"] for d in docs if len((d.get("description") or "").strip()) < 20]
        add("mongo: every description has text (needed for embeddings)", not short, f"{len(short)} too short")
        add("mongo: every book has an author", all(d.get("authors") for d in docs),
            f"{sum(1 for d in docs if not d.get('authors'))} without")
        titles = dict(c.execute("SELECT id, title FROM books").fetchall())
        add("mongo and postgres agree on titles", all(titles.get(d["_id"]) == d["title"] for d in docs),
            f"{sum(1 for d in docs if titles.get(d['_id']) != d['title'])} differ")

        for s, r in derived.status(c).items():
            add(f"registry: {s} current", r["stale"] == 0 and r["missing"] == 0 and r["orphans"] == 0,
                f"stale {r['stale']}, missing {r['missing']}, orphans {r['orphans']}")
        add("pgvector: one vector per book", pgvector_store.count(c) == n, f"{pgvector_store.count(c)} of {n}")
        add("fts: one document per book", fts_store.count(c) == n, f"{fts_store.count(c)} of {n}")
        pairs = c.execute("SELECT count(*) FROM (SELECT DISTINCT member_id, book_id FROM loans) x").fetchone()[0]

    try:
        nq = qdrant_store.connect().count(qdrant_store.COLLECTION).count
        add("qdrant: one point per book", nq == n, f"{nq} of {n}")
        drv = neo4j_graph.connect()
        with drv.session() as s:
            nb = s.run("MATCH (b:Book) RETURN count(b) AS c").single()["c"]
            ne = s.run("MATCH (:Member)-[x:BORROWED]->(:Book) RETURN count(x) AS c").single()["c"]
        drv.close()
        add("neo4j: one node per book", nb == n, f"{nb} of {n}")
        add("neo4j: BORROWED edges = distinct (member, book) loans", ne == pairs, f"{ne} edges, {pairs} pairs")
    except Exception as e:  # noqa: BLE001
        add("qdrant / neo4j reachable", False, str(e)[:80])

    lv = labels.verify()
    add("labels frozen and unchanged", lv["ok"], str(lv["files"]))
    return out
