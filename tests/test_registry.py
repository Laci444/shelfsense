"""Hybrid search, the derived-state registry and refresh. Qdrant and Neo4j are the real instances; PostgreSQL,
MongoDB and Redis are the test databases. Every test uses a throw-away book id and removes it again."""
import pytest

from shelfsense import decision, derived, retrieval
from shelfsense.stores import fts_store, postgres

TMP = "TEST_REGISTRY_BOOK"
BOOK = {"id": TMP, "title": "Test Bees", "authors": ["Test Author"], "year": 2020, "genre": "science",
        "description": "Honey bees, hives and pollination.", "book_type": "paperback"}


@pytest.fixture()
def adv():
    with postgres.connect() as c:
        postgres.load(c)
        c.execute("TRUNCATE derived_state, book_fts")
        derived.adopt(c)  # the other books count as current, so a refresh touches only the test book
    a = decision.Advisor()
    a.propagate = True
    a.redis.flushdb()
    yield a
    a.propagate = True
    try:
        a.delete_book(TMP)
    except LookupError:
        pass
    assert not a.derived_errors
    a.close()


def versions(a):
    return {s: (derived.entry(a.pg, s, TMP) or (None,))[0] for s in derived.STORES}


def test_rrf_prefers_ids_ranked_well_in_both_lists():
    fused = retrieval.rrf([("a", 9), ("b", 8), ("c", 7)], [("c", 5), ("b", 4), ("d", 3)])
    assert {i for i, _ in fused[:2]} == {"b", "c"} and {i for i, _ in fused} == {"a", "b", "c", "d"}


def test_tsquery_drops_short_and_stop_words():
    assert fts_store.tsquery("The rise of a Roman empire!") == "rise | roman | empire"
    assert fts_store.tsquery("a of") == ""


def test_fts_and_hybrid_find_a_new_book(adv):
    adv.add_book({**BOOK, "description": "Zymurgy fermentation of quince cider."})
    assert fts_store.search(adv.pg, "quince cider fermentation")[0][0] == TMP
    assert fts_store.search(adv.pg, "quince cider", genre="history") == []
    for mode in ("keyword", "hybrid"):
        found = [h["book_id"] for h in adv.recommend("quince cider fermentation zymurgy", mode=mode, limit=5)]
        assert TMP in found
    with pytest.raises(ValueError):
        retrieval.search(adv.qdrant, adv.pg, "x", mode="nope")


def test_new_book_is_current_everywhere_and_a_borrow_does_not_make_it_stale(adv):
    adv.add_book(BOOK, copies=2)
    assert set(versions(adv).values()) == {1}
    adv.pg.execute("UPDATE books SET available = available - 1 WHERE id = %s", (TMP,))
    assert adv.pg.execute("SELECT version FROM books WHERE id=%s", (TMP,)).fetchone()[0] == 1
    assert all(r["stale"] == 0 and r["missing"] == 0 for r in derived.status(adv.pg).values())


def test_missed_update_is_stale_and_refresh_repairs_only_that_book(adv):
    adv.add_book(BOOK)
    adv.propagate = False
    adv.update_book(TMP, description="Submarine engineering and deep sea pressure hulls.")
    adv.propagate = True
    assert adv.pg.execute("SELECT version FROM books WHERE id=%s", (TMP,)).fetchone()[0] == 2
    assert versions(adv) == {s: 1 for s in derived.STORES}
    assert all(TMP in derived.pending(adv.pg, s) for s in derived.STORES)
    assert TMP not in [i for i, _ in fts_store.search(adv.pg, "submarine hulls")]

    res = adv.refresh()
    assert res["refreshed"]["qdrant"] == 1 and res["refreshed"]["neo4j"] == 1
    assert versions(adv) == {s: 2 for s in derived.STORES}
    assert fts_store.search(adv.pg, "submarine hulls")[0][0] == TMP
    assert adv.refresh()["refreshed"] == {s: 0 for s in derived.STORES}


def test_metadata_change_keeps_pgvector_and_fts_current(adv):
    adv.add_book(BOOK)
    adv.update_book(TMP, genre="nature")
    assert versions(adv) == {s: 2 for s in derived.STORES}


def test_delete_forgets_registry_and_orphans_are_detected(adv):
    adv.add_book(BOOK)
    adv.delete_book(TMP)
    assert versions(adv) == {s: None for s in derived.STORES}
    adv.pg.execute("INSERT INTO derived_state (store, source_id, source_version) VALUES ('fts', %s, 1)", (TMP,))
    assert derived.orphans(adv.pg, "fts") == [TMP]
    assert adv.refresh(["fts"])["orphans_removed"]["fts"] == 1
    assert derived.orphans(adv.pg, "fts") == []
