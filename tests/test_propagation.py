"""Derived stores follow PostgreSQL/MongoDB at once. Neo4j and Qdrant are the real instances here, so every
test uses a throw-away book id and removes it again (and with it all edges)."""
import pytest

from shelfsense import decision
from shelfsense.stores import pgvector_store, postgres, qdrant_store

TMP = "TEST_PROPAGATION_BOOK"
BOOK = {"id": TMP, "title": "Test Bees", "authors": ["Test Author"], "year": 2020, "genre": "science",
        "description": "Honey bees, hives and pollination.", "book_type": "paperback"}


@pytest.fixture()
def adv():
    with postgres.connect() as c:
        postgres.load(c)
    a = decision.Advisor()
    a.propagate = True
    a.redis.flushdb()
    yield a
    try:
        a.delete_book(TMP)
    except LookupError:
        pass
    except ValueError:  # still on loan
        a.pg.execute("UPDATE loans SET returned_at = now() WHERE book_id=%s", (TMP,))
        a.delete_book(TMP)
    assert not a.derived_errors
    a.close()


def point(a):
    pts = a.qdrant.retrieve(qdrant_store.COLLECTION, [qdrant_store.point_id(TMP)], with_vectors=True)
    return pts[0] if pts else None


def node(a):
    with a.neo.session() as s:
        return s.run("MATCH (b:Book {source_id:$id}) OPTIONAL MATCH (b)-[:IN_GENRE]->(g) "
                     "RETURN b.title AS title, g.name AS genre", id=TMP).single()


def edge(a, member):
    with a.neo.session() as s:
        rec = s.run("MATCH (:Member {source_id:$m})-[x:BORROWED]->(:Book {source_id:$b}) "
                    "RETURN x.returned_at IS NOT NULL AS r", m=member, b=TMP).single()
    return None if rec is None else ("returned" if rec["r"] else "active")


def test_add_update_delete_book_reaches_neo4j_and_qdrant(adv):
    adv.add_book(BOOK, copies=2)
    assert point(adv).payload["title"] == "Test Bees" and node(adv)["genre"] == "science"
    assert adv.mongo["books"].find_one({"_id": TMP})["pages"] > 0        # type fields were filled in
    v0 = point(adv).vector
    pv0 = pgvector_store.get(adv.pg, TMP)
    assert pv0 is not None and len(pv0) == 384

    adv.update_book(TMP, genre="nature")                                    # no text change: no re-embedding
    assert node(adv)["genre"] == "nature" and point(adv).payload["genre"] == "nature"
    assert point(adv).vector == v0 and pgvector_store.get(adv.pg, TMP) == pv0

    adv.update_book(TMP, title="Deep Sea Submarines", description="Submarine engineering below the waves.")
    assert node(adv)["title"] == "Deep Sea Submarines" and point(adv).vector != v0
    assert pgvector_store.get(adv.pg, TMP) != pv0
    assert adv.pg.execute("SELECT title FROM books WHERE id=%s", (TMP,)).fetchone()[0] == "Deep Sea Submarines"

    adv.delete_book(TMP)
    assert point(adv) is None and node(adv) is None and pgvector_store.get(adv.pg, TMP) is None
    assert adv.mongo["books"].count_documents({"_id": TMP}) == 0
    assert adv.pg.execute("SELECT count(*) FROM books WHERE id=%s", (TMP,)).fetchone()[0] == 0


def test_borrow_and_return_update_neo4j(adv):
    adv.add_book(BOOK, copies=1)
    assert edge(adv, "S_STD") is None
    assert adv.borrow("S_STD", TMP)["outcome"] == "allow"
    assert edge(adv, "S_STD") == "active"
    adv.return_book("S_STD", TMP)
    assert edge(adv, "S_STD") == "returned"


def test_waitlist_promotion_creates_the_edge_and_waitlisting_does_not(adv):
    adv.add_book(BOOK, copies=1)
    adv.borrow("S_STD", TMP)
    assert adv.borrow("S_PREM", TMP)["outcome"] == "waitlist"
    assert edge(adv, "S_PREM") is None
    assert adv.return_book("S_STD", TMP)["promoted"] == "S_PREM"
    assert edge(adv, "S_PREM") == "active" and edge(adv, "S_STD") == "returned"


def test_delete_refused_while_on_loan_and_unknown_book(adv):
    adv.add_book(BOOK)
    adv.borrow("S_STD", TMP)
    with pytest.raises(ValueError):
        adv.delete_book(TMP)
    assert point(adv) is not None and node(adv) is not None             # nothing was removed anywhere
    with pytest.raises(LookupError):
        adv.update_book("NO_SUCH_BOOK", title="x")


def test_invalid_book_is_rejected_before_anything_changes(adv):
    with pytest.raises(Exception):
        adv.add_book({**BOOK, "book_type": "scroll"})
    assert adv.pg.execute("SELECT count(*) FROM books WHERE id=%s", (TMP,)).fetchone()[0] == 0
    assert point(adv) is None
