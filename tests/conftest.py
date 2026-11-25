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
        c.execute(SCHEMA.read_text(encoding="utf-8"))   # idempotent: creates or upgrades the test tables
    from shelfsense.stores import mongo, postgres
    with postgres.connect() as c:
        postgres.load(c)
    db = mongo.connect()
    yield
    from shelfsense.stores import neo4j_graph
    with psycopg.connect(admin_dsn) as real:   # the real loans of the demo members are the truth to restore
        keep = [(r[0], r[1]) for r in real.execute(
            "SELECT DISTINCT member_id, book_id FROM loans WHERE member_id LIKE 'S\\_%'")]
    drv = neo4j_graph.connect()
    with drv.session() as s:   # the tests use the real Neo4j; reset the demo members' edges to the real data
        s.run("MATCH (m:Member)-[x:BORROWED]->() WHERE m.source_id STARTS WITH 'S_' DELETE x")
        for m, b in keep:
            s.run("MERGE (m:Member {source_id: $m}) WITH m MATCH (b:Book {source_id: $b}) MERGE (m)-[:BORROWED]->(b)",
                  m=m, b=b)
    drv.close()
