"""Safety and duplicate cases (counted: see the parametrized lists). Hostile or repeated input must never
corrupt the sources: no exception escapes as a crash, no row is created by a refused request, counts stay exact."""
import threading

import pytest

from shelfsense import check, decision, scenario
from shelfsense.stores import postgres

S = scenario.books()

HOSTILE = ["'; DROP TABLE books; --", "' OR '1'='1", "S_STD'--", "", "   ", "\x00", "a" * 5000,
           "Robert'); DELETE FROM loans;--", "%", "../../etc/passwd", "ünïcödé-ID", "NULL"]


@pytest.fixture()
def conn():
    with postgres.connect() as c:
        postgres.load(c)
        yield c


def counts(c):
    return tuple(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("books", "members", "loans", "waitlist"))


@pytest.mark.parametrize("bad", HOSTILE, ids=lambda s: repr(s[:12]))
@pytest.mark.parametrize("role", ["member", "book"])
def test_hostile_ids_are_data_not_code(conn, bad, role):
    before = counts(conn)
    member, book = ("S_STD", bad) if role == "book" else (bad, S["free1"])
    if "\x00" in bad:  # PostgreSQL text cannot hold NUL: refused by the driver, nothing written
        with pytest.raises(Exception):
            postgres.borrow(conn, member, book)
        conn.rollback()
    else:
        outcome, *_ = postgres.borrow(conn, member, book)
        assert outcome == "needs_info"
    assert counts(conn) == before


@pytest.mark.parametrize("member,book", [("S_STD", "NOPE"), ("NOBODY", S["free1"]), ("NOBODY", "NOPE")])
def test_unknown_ids_need_info_and_are_recorded(conn, member, book):
    before = counts(conn)
    outcome, _, did = postgres.borrow(conn, member, book)
    assert outcome == "needs_info" and counts(conn) == before
    assert conn.execute("SELECT requested_member, requested_book FROM decisions WHERE id=%s", (did,)).fetchone() == (member, book)


def test_suspended_member_never_gets_a_loan(conn):
    before = counts(conn)
    assert postgres.borrow(conn, "S_SUSP", S["free1"])[0] == "reject"
    assert counts(conn) == before


def test_same_member_borrowing_twice_gets_one_loan(conn):
    assert postgres.borrow(conn, "S_STD", S["free1"])[0] == "allow"
    assert postgres.borrow(conn, "S_STD", S["free1"])[0] != "allow"
    n = conn.execute("SELECT count(*) FROM loans WHERE member_id='S_STD' AND book_id=%s AND returned_at IS NULL",
                     (S["free1"],)).fetchone()[0]
    assert n == 1


def test_waiting_twice_keeps_one_waitlist_row(conn):
    for _ in range(3):
        postgres.borrow(conn, "S_STD", S["none"])
    n = conn.execute("SELECT count(*) FROM waitlist WHERE member_id='S_STD' AND book_id=%s", (S["none"],)).fetchone()[0]
    assert n == 1


def test_concurrent_double_borrow_by_one_member(conn):
    res, barrier = [], threading.Barrier(4)

    def run():
        with postgres.connect() as c:
            barrier.wait()
            try:
                res.append(postgres.borrow(c, "S_STD", S["free1"])[0])
            except Exception as e:  # noqa: BLE001
                res.append(type(e).__name__)

    ts = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res.count("allow") == 1, res
    assert conn.execute("SELECT count(*) FROM loans WHERE member_id='S_STD' AND book_id=%s", (S["free1"],)).fetchone()[0] == 1
    assert conn.execute("SELECT available FROM books WHERE id=%s", (S["free1"],)).fetchone()[0] == \
        conn.execute("SELECT total - count(*) FROM books b LEFT JOIN loans l ON l.book_id=b.id AND l.returned_at IS NULL "
                     "WHERE b.id=%s GROUP BY b.total", (S["free1"],)).fetchone()[0]


def test_returning_twice_or_without_loan_is_refused(conn):
    postgres.borrow(conn, "S_STD", S["free1"])
    postgres.return_book(conn, "S_STD", S["free1"])
    before = counts(conn)
    for member, book in (("S_STD", S["free1"]), ("S_PREM", S["free1"]), ("NOBODY", "NOPE")):
        with pytest.raises(LookupError):
            postgres.return_book(conn, member, book)
    assert counts(conn) == before
    assert conn.execute("SELECT available <= total FROM books WHERE id=%s", (S["free1"],)).fetchone()[0]


def test_adding_a_duplicate_book_id_leaves_the_original_untouched():
    with postgres.connect() as c:
        postgres.load(c)
    a = decision.Advisor()
    try:
        orig = a.mongo["books"].find_one({"_id": S["free1"]})
        dup = {"id": S["free1"], "title": "Impostor", "authors": ["X"], "year": 2000, "genre": "science",
               "description": "a duplicate id that must be refused", "book_type": "paperback"}
        with pytest.raises(Exception):
            a.add_book(dup)
        assert a.mongo["books"].find_one({"_id": S["free1"]}) == orig
        assert a.pg.execute("SELECT title FROM books WHERE id=%s", (S["free1"],)).fetchone()[0] == orig["title"]
    finally:
        a.close()


def test_data_checks_pass_on_a_clean_load_and_catch_corruption(conn):
    results = {n: ok for n, ok, _ in check._pg_checks(conn)}
    assert results["postgres: available = total - active loans"] and results["postgres: no duplicate active loan (member, book)"]
    conn.execute("UPDATE books SET available = available - 1 WHERE id = %s AND available > 0", (S["free1"],))
    results = {n: ok for n, ok, _ in check._pg_checks(conn)}
    assert not results["postgres: available = total - active loans"]
