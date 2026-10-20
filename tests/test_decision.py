import pytest

from shelfsense import decision, scenario
from shelfsense.stores import postgres, redis_cache

S = scenario.books()


@pytest.fixture()
def adv():
    with postgres.connect() as c:
        postgres.load(c)
    a = decision.Advisor()
    a.redis.flushdb()
    yield a
    a.close()


def test_check_is_read_only(adv):
    last = S["last"]
    before = adv.pg.execute("SELECT available FROM books WHERE id=%s", (last,)).fetchone()[0]
    assert postgres.check(adv.pg, "S_STD", last) == ("allow", "copy available")
    assert adv.pg.execute("SELECT available FROM books WHERE id=%s", (last,)).fetchone()[0] == before
    assert adv.pg.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_cache_aside(adv):
    free = S["free2"]
    assert adv.decide_cached("S_STD", free) == ("allow", "copy available", False)
    assert adv.decide_cached("S_STD", free) == ("allow", "copy available", True)
    assert 0 < adv.redis.ttl(redis_cache.decision_key("S_STD", free)) <= 300
    cached = redis_cache.get_decision(adv.redis, "S_STD", free)
    assert cached["source_id"] == free and cached["refreshed_at"] > 0


def test_borrow_invalidates_cache_and_changes_decision(adv):
    last = S["last"]
    adv.decide_cached("S_STD", last)          # allow (1 copy left), cached
    adv.decide_cached("S_PREM", last)
    res = adv.borrow("S_STD", last)           # takes the last copy
    assert res["outcome"] == "allow" and res["decision_id"] >= 1
    assert redis_cache.get_decision(adv.redis, "S_PREM", last) is None
    assert adv.decide_cached("S_PREM", last) == ("waitlist", "no copy available", False)


def test_advise_waitlist_gives_available_alternatives(adv):
    none = S["none"]
    r = adv.advise("S_STD", none)             # no copies left
    assert r["outcome"] == "waitlist" and r["alternatives"]
    ids = [a["book_id"] for a in r["alternatives"]]
    assert none not in ids and len(ids) == len(set(ids))
    assert all(a["available"] > 0 and a["authors"] for a in r["alternatives"])


def test_advise_allow_has_no_alternatives(adv):
    assert "alternatives" not in adv.advise("S_STD", S["free2"])


def test_recommend_filters_genre_and_availability(adv):
    recs = adv.recommend("epic fantasy with dragons and magic", genre="fantasy")
    assert len(recs) == 5 and {r["genre"] for r in recs} == {"fantasy"}
    assert all(r["available"] > 0 for r in recs)
