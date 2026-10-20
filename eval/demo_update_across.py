"""Demo: one change, five stores - propagated automatically.

Through the Advisor a book is edited, a loan is made and returned, a book is added and deleted.
After each step the demo reads Qdrant and Neo4j directly to show that they followed PostgreSQL/MongoDB
without a sync, and that only the changed book was re-embedded.
Run: shelfsense eval update-across      (afterwards the demo restores everything it touched)
"""
import time

from shelfsense import decision, scenario
from shelfsense.stores import postgres, qdrant_store

BOOK, MEMBER = scenario.books()["free2"], "S_STD"
NEW_TITLE = "Deep Sea Engineering"
NEW_TEXT = "A completely rewritten blurb about deep sea diving and submarine engineering."
TMP = "DEMO_NEW_BOOK"


def _qdrant(a, book=BOOK):
    pts = a.qdrant.retrieve(qdrant_store.COLLECTION, [qdrant_store.point_id(book)], with_vectors=True)
    return pts[0] if pts else None


def _neo_title(a, book=BOOK):
    with a.neo.session() as s:
        rec = s.run("MATCH (b:Book {source_id:$id}) RETURN b.title AS t", id=book).single()
    return rec["t"] if rec else None


def _edge(a):
    with a.neo.session() as s:
        rec = s.run("MATCH (:Member {source_id:$m})-[x:BORROWED]->(:Book {source_id:$b}) "
                    "RETURN x.returned_at IS NOT NULL AS returned", m=MEMBER, b=BOOK).single()
    return None if rec is None else ("returned" if rec["returned"] else "active")


def run(verbose=True):
    say = print if verbose else (lambda *a: None)
    with postgres.connect() as c:
        postgres.load(c)
    a = decision.Advisor()
    a.propagate = True
    a.redis.flushdb()
    original = {k: a.mongo["books"].find_one({"_id": BOOK})[k] for k in ("title", "description")}
    out = {}
    try:
        q0 = _qdrant(a)
        out["before"] = {"qdrant_title": q0.payload["title"], "neo4j_title": _neo_title(a), "neo4j_edge": _edge(a)}

        a.decide_cached(MEMBER, BOOK)
        a.update_book(BOOK, title=NEW_TITLE, description=NEW_TEXT)
        q1 = _qdrant(a)
        out["after_update_book"] = {
            "redis_decision_invalidated": not a.redis.exists(f"decision:{MEMBER}:{BOOK}"),
            "qdrant_vector_changed": q1.vector != q0.vector, "qdrant_title": q1.payload["title"],
            "qdrant_refreshed_at_moved": q1.payload["refreshed_at"] > q0.payload["refreshed_at"],
            "neo4j_title": _neo_title(a)}

        a.borrow(MEMBER, BOOK)
        out["after_borrow"] = {"neo4j_edge": _edge(a)}
        a.return_book(MEMBER, BOOK)
        out["after_return"] = {"neo4j_edge": _edge(a)}

        a.add_book({"id": TMP, "title": "Demo: A Book About Bees", "authors": ["Demo Author"], "year": 2024,
                    "genre": "science", "description": "Honey bees, hives and pollination.",
                    "book_type": "paperback"}, copies=2)
        out["after_add_book"] = {"qdrant_has_point": _qdrant(a, TMP) is not None, "neo4j_title": _neo_title(a, TMP),
                                 "postgres_copies": a.pg.execute("SELECT total FROM books WHERE id=%s", (TMP,)).fetchone()[0]}
        a.delete_book(TMP)
        out["after_delete_book"] = {"qdrant_has_point": _qdrant(a, TMP) is not None,
                                    "neo4j_title": _neo_title(a, TMP),
                                    "mongo_has_doc": a.mongo["books"].count_documents({"_id": TMP}) > 0}
        out["derived_errors"] = list(a.derived_errors)
        for step, vals in out.items():
            say(f"{step}:")
            for k, v in (vals.items() if isinstance(vals, dict) else [("", vals)]):
                say(f"    {k}: {v}")
    finally:
        try:
            a.delete_book(TMP)
        except LookupError:
            pass
        a.update_book(BOOK, **original)
        with a.neo.session() as s:
            s.run("MATCH (:Member {source_id:$m})-[x:BORROWED]->(:Book {source_id:$b}) DELETE x",
                  m=MEMBER, b=BOOK).consume()
        a.close()
        with postgres.connect() as c:
            postgres.load(c)
        decision.Advisor().redis.flushdb()
    say("(demo data restored: all five stores are consistent again)")
    return out


if __name__ == "__main__":
    run()
