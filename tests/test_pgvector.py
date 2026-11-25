import numpy as np
import pytest

from eval import demo_rules
from shelfsense import decision, rules
from shelfsense.stores import pgvector_store as pv
from shelfsense.stores import postgres


def unit(seed):
    v = np.random.default_rng(seed).normal(size=384)
    return v / np.linalg.norm(v)


@pytest.fixture()
def conn():
    with postgres.connect() as c:
        postgres.load(c)
        c.execute("TRUNCATE book_vectors")
        ids = [r[0] for r in c.execute("SELECT id FROM books ORDER BY id LIMIT 40")]
        pv.rebuild(c, ids, [unit(i) for i in range(len(ids))], m=8, ef_construction=32)
        c.ids = ids
        yield c
        c.execute("TRUNCATE book_vectors")


def test_rebuild_and_exact_search_matches_numpy(conn):
    assert pv.count(conn) == 40
    q = unit(1000)
    expected = sorted(range(40), key=lambda i: -float(q @ unit(i)))[:5]
    got = pv.search(conn, q, limit=5, exact=True)
    assert [g[0] for g in got] == [conn.ids[i] for i in expected]
    assert got[0][1] == pytest.approx(float(q @ unit(expected[0])), abs=1e-4)


def test_ann_search_uses_the_index_and_finds_the_exact_neighbour(conn):
    q = unit(7)                                           # identical to the stored vector number 7
    assert pv.search(conn, q, limit=1, ef_search=40)[0][0] == conn.ids[7]
    conn.execute("SET LOCAL enable_seqscan = off")            # 40 rows: the planner would otherwise scan
    plan = conn.execute("EXPLAIN SELECT book_id FROM book_vectors ORDER BY embedding <=> %s::vector LIMIT 3",
                        (pv._lit(q),)).fetchall()
    assert any("book_vectors_hnsw" in r[0] for r in plan)


def test_filters_come_from_the_books_table(conn):
    q = unit(3)
    genre = conn.execute("SELECT genre FROM books WHERE id=%s", (conn.ids[3],)).fetchone()[0]
    hits = pv.search(conn, q, limit=10, genre=genre)
    assert hits and all(conn.execute("SELECT genre FROM books WHERE id=%s", (h[0],)).fetchone()[0] == genre
                        for h in hits)
    year = conn.execute("SELECT max(year) FROM books").fetchone()[0]
    assert all(conn.execute("SELECT year FROM books WHERE id=%s", (h[0],)).fetchone()[0] >= year
               for h in pv.search(conn, q, limit=10, year_from=year))


def test_upsert_replaces_and_delete_removes(conn):
    b = conn.ids[0]
    pv.upsert_book(conn, b, unit(99))
    assert pv.count(conn) == 40 and np.allclose(pv.get(conn, b), unit(99), atol=1e-5)
    pv.delete_book(conn, b)
    assert pv.get(conn, b) is None and pv.count(conn) == 39


def test_demo_rules_shows_the_old_decision_under_its_version():
    out = demo_rules.run(verbose=False)
    assert (out["v1_outcome"], out["v2_outcome"]) == ("reject", "allow")
    assert out["old_decision"] == {"rule_version": 1, "replay_ok": True, "active_version_at_explain": 2}
    assert out["new_decision"] == {"rule_version": 2, "replay_ok": True}
    with postgres.connect() as c:
        assert rules.active(c)["version"] == 1 and len(rules.history(c)) == 1
