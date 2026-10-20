"""Tests never touch the real data: Postgres/Mongo/Redis are redirected to test databases
(see shelfsense/config.py), so the expensive `sync` of Neo4j and Qdrant is never needed afterwards."""
import os
from pathlib import Path

os.environ["SHELFSENSE_TEST"] = "1"

import psycopg  # noqa: E402
import pytest  # noqa: E402

from shelfsense import config  # noqa: E402

SCHEMA = Path(__file__).resolve().parents[1] / "shelfsense" / "sql" / "schema.sql"


@pytest.fixture(scope="session", autouse=True)
def test_databases():
    admin_dsn = config.PG_DSN.rsplit("/", 1)[0] + "/shelfsense"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname='shelfsense_test'").fetchone():
            admin.execute("CREATE DATABASE shelfsense_test")
    with psycopg.connect(config.PG_DSN) as c:
        if not c.execute("SELECT to_regclass('public.books')").fetchone()[0]:
            c.execute(SCHEMA.read_text(encoding="utf-8"))
    from shelfsense.stores import mongo, postgres
    with postgres.connect() as c:
        postgres.load(c)
    db = mongo.connect()
    mongo.load(db)
    yield
