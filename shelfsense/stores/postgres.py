"""PostgreSQL store: schema, loader, the versioned decision and the loan transaction."""
import json
from pathlib import Path

import psycopg

from shelfsense import rules
from shelfsense.config import DATA_DIR, PG_DSN

GEN = DATA_DIR / "generated"
SCHEMA = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"


def connect():
    return psycopg.connect(PG_DSN)


def apply_schema(conn):
    """Create or upgrade the tables (schema.sql is idempotent); data is kept."""
    conn.execute(SCHEMA.read_text(encoding="utf-8"))
    if not conn.autocommit:
        conn.commit()


def load(conn):
    """Reload all business data from data/generated (idempotent). Rules are kept."""
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


def gather(conn, member_id, book_id, lock=False):
    """The evidence of one decision, each value with its source id and row version.

    Returns a JSON-serialisable dict; `member` / `book` are None when the id is unknown.
    """
    book = conn.execute(
        "SELECT id, version, book_type, available FROM books WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (book_id,)).fetchone()
    member = conn.execute(
        "SELECT id, version, tier, suspended FROM members WHERE id = %s", (member_id,)).fetchone()
    inputs = {"member": None, "book": None}
    if member:
        inputs["member"] = dict(zip(("id", "version", "tier", "suspended"), member))
    if book:
        inputs["book"] = dict(zip(("id", "version", "book_type", "available"), book))
    if member and book:
        active, holds = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE book_id = %s) FROM loans "
            "WHERE member_id = %s AND returned_at IS NULL", (book_id, member_id)).fetchone()
        inputs["active_loans"] = active
        inputs["holds_book"] = holds > 0
        inputs["on_waitlist"] = conn.execute(
            "SELECT 1 FROM waitlist WHERE member_id = %s AND book_id = %s", (member_id, book_id)).fetchone() is not None
    return inputs


def _decide(conn, member_id, book_id, lock):
    """Gather the evidence and apply the active rules. Returns a decision dict (nothing is written)."""
    rs = rules.active(conn)
    inputs = gather(conn, member_id, book_id, lock)
    outcome, reason, rule_id = rules.decide(inputs, rs["params"])
    return {"outcome": outcome, "reason": reason, "rule_id": rule_id, "rule_version": rs["version"],
            "inputs": inputs, "params": rs["params"]}


def trace(conn, member_id, book_id, lock=False):
    """Every loan rule in the order they are applied, with whether it passed (see rules.evaluate)."""
    d = _decide(conn, member_id, book_id, lock)
    return rules.evaluate(d["inputs"], d["params"])


def _record(conn, d, member_id, book_id, reason=None):
    ok = d["inputs"]["member"] is not None and d["inputs"]["book"] is not None
    return conn.execute(
        "INSERT INTO decisions (member_id, book_id, requested_member, requested_book, outcome, reason, "
        "rule_version, rule_id, inputs) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
        (member_id if ok else None, book_id if ok else None, member_id, book_id, d["outcome"],
         reason or d["reason"], d["rule_version"], d["rule_id"], json.dumps(d["inputs"]))).fetchone()[0]


def check(conn, member_id, book_id):
    """Read-only decision: what would borrow() answer right now? No locks, no writes."""
    d = _decide(conn, member_id, book_id, lock=False)
    return d["outcome"], d["reason"]


def borrow(conn, member_id, book_id):
    """Decide and record a loan in ONE transaction. Returns (outcome, reason, decision_id).

    The book row is locked with SELECT ... FOR UPDATE, so two members taking
    the last copy are serialised: the first commits, the second sees available = 0.
    The decision is stored in the same transaction as its effect, with the rule version and the
    evidence it was based on.
    """
    with conn.transaction():
        d = _decide(conn, member_id, book_id, lock=True)
        if d["outcome"] == "waitlist":
            conn.execute("INSERT INTO waitlist (member_id, book_id) VALUES (%s,%s)", (member_id, book_id))
        elif d["outcome"] == "allow":
            conn.execute("UPDATE books SET available = available - 1 WHERE id = %s", (book_id,))
            conn.execute(
                "INSERT INTO loans (member_id, book_id, due_at) VALUES (%s,%s, now() + make_interval(days => %s))",
                (member_id, book_id, d["params"]["loan_days"]))
        decision_id = _record(conn, d, member_id, book_id)
    return d["outcome"], d["reason"], decision_id


def explain_decision(conn, decision_id):
    """Explain a stored decision from its own rows: the stored evidence is re-evaluated with the
    rule version stored with it (not the active one). `replay_ok` says the outcome is reproduced."""
    row = conn.execute(
        "SELECT id, decided_at, requested_member, requested_book, outcome, reason, rule_version, rule_id, inputs "
        "FROM decisions WHERE id = %s", (decision_id,)).fetchone()
    if row is None:
        raise LookupError("unknown decision")
    dec = dict(zip(("id", "decided_at", "member", "book", "outcome", "reason", "rule_version", "rule_id", "inputs"), row))
    rs = rules.get(conn, dec["rule_version"])
    active = rules.active(conn)["version"]
    items = rules.evaluate(dec["inputs"], rs["params"])
    if dec["reason"] == "promoted from waitlist":  # the stored reason is the promotion, the rules said allow
        replay = "allow"
    else:
        replay = next((i["outcome"] for i in items if not i["ok"]), "allow")
    return {"decision": dec, "rules": rs, "active_version": active, "trace": items,
            "replay_outcome": replay, "replay_ok": replay == dec["outcome"]}


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
                    d = _decide(conn, wm, book_id, lock=True)
                    if d["outcome"] != "allow":
                        raise _Skip
                    conn.execute("UPDATE books SET available = available - 1 WHERE id = %s", (book_id,))
                    conn.execute("INSERT INTO loans (member_id, book_id, due_at) "
                                 "VALUES (%s,%s, now() + make_interval(days => %s))",
                                 (wm, book_id, d["params"]["loan_days"]))
                    did = _record(conn, d, wm, book_id, reason="promoted from waitlist")
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
    m = list(m)
    m[3] = rules.active(conn)["params"]["max_loans"].get(m[2], m[3])  # the active rules set the limit
    return {"member": dict(zip(("id", "name", "tier", "max_loans", "suspended"), m)),
            "loans": [list(r) for r in loans], "waitlist": [list(r) for r in waiting],
            "history": [list(r) for r in history]}


def book_waitlist(conn, book_id):
    return [list(r) for r in conn.execute(
        "SELECT w.member_id, m.name, w.created_at FROM waitlist w JOIN members m ON m.id = w.member_id "
        "WHERE w.book_id = %s ORDER BY w.created_at, w.id", (book_id,))]


def recent_decisions(conn, limit=30):
    return [list(r) for r in conn.execute(
        "SELECT d.id, d.decided_at, d.requested_member, d.requested_book, coalesce(b.title, '?'), d.outcome, "
        "d.reason, d.rule_version FROM decisions d LEFT JOIN books b ON b.id = d.book_id "
        "ORDER BY d.id DESC LIMIT %s", (limit,))]