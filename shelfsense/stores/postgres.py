"""PostgreSQL store: loader and the loan transaction."""
import json
from pathlib import Path

import psycopg

from shelfsense.config import DATA_DIR, PG_DSN

GEN = DATA_DIR / "generated"


def connect():
    return psycopg.connect(PG_DSN)


def load(conn):
    """Reload all business data from data/generated (idempotent)."""
    rd = lambda n: json.loads((GEN / f"{n}.json").read_text(encoding="utf-8"))
    with conn.transaction():
        conn.execute("TRUNCATE decisions, waitlist, loans, books, members RESTART IDENTITY CASCADE")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO members (id, name, tier, max_loans, suspended) VALUES (%s,%s,%s,%s,%s)",
                [(m["id"], m["name"], m["tier"], m["max_loans"], m["suspended"]) for m in rd("members")])
            cur.executemany(
                "INSERT INTO books (id, title, genre, year, book_type, available, total) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [(b["id"], b["title"], b["genre"], b["year"], b["book_type"], b["available"], b["total"])
                 for b in rd("books")])
            cur.executemany(
                "INSERT INTO loans (member_id, book_id, loaned_at, due_at, returned_at) VALUES (%s,%s,%s,%s,%s)",
                [(l["member_id"], l["book_id"], l["loaned_at"], l["due_at"], l["returned_at"]) for l in rd("loans")])
            cur.executemany(
                "INSERT INTO waitlist (member_id, book_id, created_at) VALUES (%s,%s,%s)",
                [(w["member_id"], w["book_id"], w["created_at"]) for w in rd("waitlist")])


def trace(conn, member_id, book_id, lock=False):
    """Every loan rule in the order they are applied, with whether it passed.

    Returns a list of {rule, ok, detail, outcome, reason}; `outcome`/`reason` is the answer if the rule fails.
    """
    book = conn.execute(
        "SELECT book_type, available FROM books WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (book_id,)).fetchone()
    member = conn.execute(
        "SELECT tier, max_loans, suspended FROM members WHERE id = %s", (member_id,)).fetchone()
    if book is None or member is None:
        raise LookupError("unknown member or book")
    book_type, available = book
    tier, max_loans, suspended = member
    active = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE book_id = %s) FROM loans "
        "WHERE member_id = %s AND returned_at IS NULL", (book_id, member_id)).fetchone()
    on_list = conn.execute(
        "SELECT 1 FROM waitlist WHERE member_id = %s AND book_id = %s", (member_id, book_id)).fetchone()
    return [
        {"rule": "Member is not suspended", "ok": not suspended, "detail": "suspended" if suspended else "good standing",
         "outcome": "reject", "reason": "member suspended"},
        {"rule": "Member is below the loan limit", "ok": active[0] < max_loans,
         "detail": f"{active[0]} of {max_loans} loans", "outcome": "reject", "reason": "member at max loans"},
        {"rule": "Member does not already hold the book", "ok": active[1] == 0,
         "detail": "holds a copy" if active[1] else "no copy held",
         "outcome": "reject", "reason": "member already holds this book"},
        {"rule": "Member is not already on the waitlist", "ok": not on_list,
         "detail": "already waiting" if on_list else "not waiting",
         "outcome": "reject", "reason": "member already on waitlist"},
        {"rule": "Book type is allowed for the tier", "ok": not (book_type == "journal" and tier != "premium"),
         "detail": f"{book_type}, {tier}", "outcome": "reject", "reason": "journals are premium only"},
        {"rule": "A copy is available", "ok": available > 0, "detail": f"{available} free",
         "outcome": "waitlist", "reason": "no copy available"},
    ]


def _evaluate(conn, member_id, book_id, lock):
    """Apply the loan rules (first failing rule wins). Returns (outcome, reason)."""
    for r in trace(conn, member_id, book_id, lock):
        if not r["ok"]:
            return r["outcome"], r["reason"]
    return "allow", "copy available"


def check(conn, member_id, book_id):
    """Read-only decision: what would borrow() answer right now? No locks, no writes."""
    return _evaluate(conn, member_id, book_id, lock=False)


def borrow(conn, member_id, book_id):
    """Decide and record a loan in ONE transaction. Returns (outcome, reason, decision_id).

    The book row is locked with SELECT ... FOR UPDATE, so two members taking
    the last copy are serialised: the first commits, the second sees available = 0.
    The decision is stored in the same transaction as its effect.
    """
    with conn.transaction():
        outcome, reason = _evaluate(conn, member_id, book_id, lock=True)
        if outcome == "waitlist":
            conn.execute("INSERT INTO waitlist (member_id, book_id) VALUES (%s,%s)", (member_id, book_id))
        elif outcome == "allow":
            conn.execute("UPDATE books SET available = available - 1 WHERE id = %s", (book_id,))
            conn.execute(
                "INSERT INTO loans (member_id, book_id, due_at) VALUES (%s,%s, now() + interval '21 days')",
                (member_id, book_id))
        decision_id = conn.execute(
            "INSERT INTO decisions (member_id, book_id, outcome, reason) VALUES (%s,%s,%s,%s) RETURNING id",
            (member_id, book_id, outcome, reason)).fetchone()[0]
    return outcome, reason, decision_id


class _Skip(Exception):
    pass


def return_book(conn, member_id, book_id):
    """Return a loan; the first waitlisted member who is allowed to borrow gets the copy.

    One transaction. Returns {"promoted": member_id or None, "decision_id": id or None}.
    """
    with conn.transaction():
        conn.execute("SELECT 1 FROM books WHERE id = %s FOR UPDATE", (book_id,))
        loan = conn.execute("SELECT id FROM loans WHERE member_id = %s AND book_id = %s AND returned_at IS NULL "
                            "FOR UPDATE", (member_id, book_id)).fetchone()
        if loan is None:
            raise LookupError("no active loan of this book for this member")
        conn.execute("UPDATE loans SET returned_at = now() WHERE id = %s", (loan[0],))
        conn.execute("UPDATE books SET available = available + 1 WHERE id = %s", (book_id,))
        waiting = conn.execute("SELECT id, member_id FROM waitlist WHERE book_id = %s ORDER BY created_at, id",
                               (book_id,)).fetchall()
        for wid, wm in waiting:
            try:
                with conn.transaction():  # savepoint: undone if this member may not borrow
                    conn.execute("DELETE FROM waitlist WHERE id = %s", (wid,))
                    outcome, _ = _evaluate(conn, wm, book_id, lock=True)
                    if outcome != "allow":
                        raise _Skip
                    conn.execute("UPDATE books SET available = available - 1 WHERE id = %s", (book_id,))
                    conn.execute("INSERT INTO loans (member_id, book_id, due_at) "
                                 "VALUES (%s,%s, now() + interval '21 days')", (wm, book_id))
                    did = conn.execute(
                        "INSERT INTO decisions (member_id, book_id, outcome, reason) "
                        "VALUES (%s,%s,'allow','promoted from waitlist') RETURNING id", (wm, book_id)).fetchone()[0]
            except _Skip:
                continue
            return {"promoted": wm, "decision_id": did}
    return {"promoted": None, "decision_id": None}


def leave_waitlist(conn, member_id, book_id):
    n = conn.execute("DELETE FROM waitlist WHERE member_id = %s AND book_id = %s", (member_id, book_id)).rowcount
    if not n:
        raise LookupError("member is not on the waitlist of this book")


def add_book(conn, b, copies=1):
    conn.execute("INSERT INTO books (id, title, genre, year, book_type, available, total) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (b["id"], b["title"], b["genre"], b["year"], b["book_type"], copies, copies))


def update_book(conn, book_id, fields):
    """fields: any of title, genre, year (the columns PostgreSQL keeps)."""
    cols = [k for k in ("title", "genre", "year") if k in fields]
    if not cols:
        return
    sets = ", ".join(f"{c} = %s" for c in cols)
    if not conn.execute(f"UPDATE books SET {sets} WHERE id = %s", [fields[c] for c in cols] + [book_id]).rowcount:
        raise LookupError("unknown book")


def delete_book(conn, book_id):
    """Refuses while copies are on loan; otherwise removes the book with its history."""
    with conn.transaction():
        if not conn.execute("SELECT 1 FROM books WHERE id = %s FOR UPDATE", (book_id,)).fetchone():
            raise LookupError("unknown book")
        if conn.execute("SELECT count(*) FROM loans WHERE book_id = %s AND returned_at IS NULL",
                        (book_id,)).fetchone()[0]:
            raise ValueError("book is on loan; wait for the returns")
        for t in ("decisions", "waitlist", "loans"):
            conn.execute(f"DELETE FROM {t} WHERE book_id = %s", (book_id,))
        conn.execute("DELETE FROM books WHERE id = %s", (book_id,))


def search_books(conn, text, limit=20):
    rows = conn.execute("SELECT id, title, genre, available, total, book_type FROM books "
                        "WHERE title ILIKE %s OR id = %s ORDER BY title LIMIT %s", (f"%{text}%", text, limit))
    return [dict(zip(("id", "title", "genre", "available", "total", "book_type"), r)) for r in rows]


def search_members(conn, text="", limit=30):
    """Members matching id or name; the scenario members (S_*) come first."""
    rows = conn.execute(
        "SELECT id, name, tier, suspended FROM members WHERE id ILIKE %s OR name ILIKE %s "
        "ORDER BY (id LIKE 'S\\_%%') DESC, id LIMIT %s", (f"%{text}%", f"%{text}%", limit))
    return [dict(zip(("id", "name", "tier", "suspended"), r)) for r in rows]


def genres(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT genre FROM books ORDER BY 1")]


def member_profile(conn, member_id):
    m = conn.execute("SELECT id, name, tier, max_loans, suspended FROM members WHERE id = %s",
                     (member_id,)).fetchone()
    if m is None:
        raise LookupError("unknown member")
    loans = conn.execute(
        "SELECT l.book_id, b.title, l.loaned_at::date, l.due_at::date FROM loans l JOIN books b ON b.id = l.book_id "
        "WHERE l.member_id = %s AND l.returned_at IS NULL ORDER BY l.due_at", (member_id,)).fetchall()
    waiting = conn.execute(
        "SELECT w.book_id, b.title, "
        "(SELECT count(*) FROM waitlist x WHERE x.book_id = w.book_id AND (x.created_at, x.id) <= (w.created_at, w.id))"
        " FROM waitlist w JOIN books b ON b.id = w.book_id WHERE w.member_id = %s ORDER BY w.created_at",
        (member_id,)).fetchall()
    history = conn.execute(
        "SELECT l.book_id, b.title, l.loaned_at::date, l.returned_at::date FROM loans l "
        "JOIN books b ON b.id = l.book_id WHERE l.member_id = %s AND l.returned_at IS NOT NULL "
        "ORDER BY l.returned_at DESC LIMIT 15", (member_id,)).fetchall()
    return {"member": dict(zip(("id", "name", "tier", "max_loans", "suspended"), m)),
            "loans": [list(r) for r in loans], "waitlist": [list(r) for r in waiting],
            "history": [list(r) for r in history]}


def book_waitlist(conn, book_id):
    return [list(r) for r in conn.execute(
        "SELECT w.member_id, m.name, w.created_at FROM waitlist w JOIN members m ON m.id = w.member_id "
        "WHERE w.book_id = %s ORDER BY w.created_at, w.id", (book_id,))]


def recent_decisions(conn, limit=30):
    return [list(r) for r in conn.execute(
        "SELECT d.id, d.decided_at, d.member_id, d.book_id, b.title, d.outcome, d.reason FROM decisions d "
        "JOIN books b ON b.id = d.book_id ORDER BY d.id DESC LIMIT %s", (limit,))]