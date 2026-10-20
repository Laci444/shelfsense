import json
import threading
from pathlib import Path

import pytest
from pymongo.errors import WriteError

from shelfsense import scenario
from shelfsense.config import DATA_DIR
from shelfsense.stores import mongo, postgres

S = scenario.books()
CASES = json.loads((DATA_DIR / "labels" / "loan_cases.json").read_text())


@pytest.fixture()
def conn():
    with postgres.connect() as c:
        postgres.load(c)
        yield c


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['id']}")
def test_loan_case(conn, case):
    outcome, *_ = postgres.borrow(conn, case["member"], case["book"])
    assert outcome == case["expected"], case["why"]


def test_last_copy_race():
    """Two members take the last copy concurrently: exactly one is allowed."""
    last = S["last"]
    with postgres.connect() as c:
        postgres.load(c)
    results = {}
    barrier = threading.Barrier(2)

    def run(member):
        with postgres.connect() as c:
            barrier.wait()
            results[member] = postgres.borrow(c, member, last)[0]

    # S_STD and S_PREM both eligible; book has 1 copy left
    ts = [threading.Thread(target=run, args=(m,)) for m in ("S_STD", "S_PREM")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results.values()) == ["allow", "waitlist"], results
    with postgres.connect() as c:
        assert c.execute("SELECT available FROM books WHERE id=%s", (last,)).fetchone()[0] == 0


def test_check_constraint_blocks_negative_available(conn):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE books SET available = -1 WHERE id = %s", (S["last"],))


@pytest.fixture()
def coll():
    db = mongo.connect()
    mongo.load(db)
    return db["books"]


def test_mongo_loaded(coll):
    assert coll.count_documents({}) == len(json.loads((DATA_DIR / "generated" / "books.json").read_text(encoding="utf-8")))


def test_mongo_rejects_missing_type_field(coll):
    doc = {"_id": "X1", "title": "t", "authors": ["a"], "year": 2000, "genre": "fantasy",
           "description": "d", "book_type": "ebook", "file_format": "epub"}  # size_mb missing
    with pytest.raises(WriteError):
        coll.insert_one(doc)


def test_mongo_rejects_wrong_type_fields(coll):
    doc = {"_id": "X2", "title": "t", "authors": ["a"], "year": 2000, "genre": "fantasy",
           "description": "d", "book_type": "journal", "pages": 100, "isbn": "1"}  # paperback fields
    with pytest.raises(WriteError):
        coll.insert_one(doc)


def test_mongo_accepts_valid_journal(coll):
    doc = {"_id": "X3", "title": "t", "authors": ["a"], "year": 2000, "genre": "science",
           "description": "d", "book_type": "journal", "issn": "1234-5678", "volume": 1, "issue": 2}
    assert coll.insert_one(doc).inserted_id == "X3"
    coll.delete_one({"_id": "X3"})
