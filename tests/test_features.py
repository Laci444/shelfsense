import pytest

from shelfsense import decision, scenario
from shelfsense.stores import postgres

S = scenario.books()


@pytest.fixture()
def adv():
    with postgres.connect() as c:
        postgres.load(c)
    a = decision.Advisor()
    a.redis.flushdb()
    yield a
    a.close()


def test_trace_lists_rules_in_order(adv):
    rules = adv.explain("S_SUSP", S["free1"])
    assert [r["ok"] for r in rules][0] is False and len(rules) == 6
    assert all(r["ok"] for r in adv.explain("S_STD", S["free1"]))
    last = adv.explain("S_STD", S["none"])[-1]
    assert not last["ok"] and last["outcome"] == "waitlist"


def test_return_serves_first_eligible_waitlist_member(adv):
    book = S["last"]
    adv.borrow("S_STD", book)                              # takes the last free copy (1 of 2 was free)
    assert adv.pg.execute("SELECT available FROM books WHERE id=%s", (book,)).fetchone()[0] == 0
    assert adv.borrow("S_PREM", book)["outcome"] == "waitlist"
    out = adv.return_book("S_STD", book)
    assert out["promoted"] == "S_PREM"
    assert adv.pg.execute("SELECT count(*) FROM loans WHERE member_id='S_PREM' AND book_id=%s AND "
                          "returned_at IS NULL", (book,)).fetchone()[0] == 1
    assert adv.pg.execute("SELECT available FROM books WHERE id=%s", (book,)).fetchone()[0] == 0


def test_return_skips_ineligible_waitlist_member(adv):
    book = S["none"]
    adv.pg.execute("INSERT INTO waitlist (member_id, book_id) VALUES ('S_SUSP', %s)", (book,))
    adv.pg.execute("INSERT INTO loans (member_id, book_id, due_at) VALUES ('S_STD', %s, now())", (book,))
    out = adv.return_book("S_STD", book)
    assert out["promoted"] is None
    assert adv.pg.execute("SELECT available FROM books WHERE id=%s", (book,)).fetchone()[0] == 1
    assert adv.pg.execute("SELECT count(*) FROM waitlist WHERE book_id=%s AND member_id='S_SUSP'",
                          (book,)).fetchone()[0] == 1   # kept in the queue


def test_return_without_loan_and_leave_waitlist_errors(adv):
    with pytest.raises(LookupError):
        adv.return_book("S_STD", S["free1"])
    with pytest.raises(LookupError):
        adv.leave_waitlist("S_STD", S["none"])
    adv.borrow("S_STD", S["none"])
    adv.leave_waitlist("S_STD", S["none"])
    assert adv.member_profile("S_STD")["waitlist"] == []


def test_profile_book_details_and_stats(adv):
    adv.borrow("S_STD", S["free1"])
    p = adv.member_profile("S_STD")
    assert [r[0] for r in p["loans"]] == [S["free1"]]
    d = adv.book_details(S["free1"])
    assert d["postgres"]["on_loan"] == 1 and d["mongo"]["title"] and d["neo4j"]["readers"] >= 0
    st = adv.stats()
    assert st["PostgreSQL"]["decisions"] == 1 and st["Qdrant"]["vectors"] > 0
    assert all(ok for ok, _ in adv.health().values())
    assert postgres.recent_decisions(adv.pg)[0][5] == "allow"
