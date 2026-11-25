"""Neo4j store (derived): (:Book)-[:BY]->(:Author), (:Book)-[:IN_GENRE]->(:Genre),
(:Member)-[:BORROWED]->(:Book). Nodes keep source_id and refreshed_at."""
from neo4j import GraphDatabase

from shelfsense.config import NEO4J_AUTH, NEO4J_URI


def connect():
    return GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)


def rebuild(driver, books, members, loans):
    """books: raw book dicts, members: member dicts, loans: loan dicts. Idempotent full rebuild."""
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
        for stmt in ("CREATE CONSTRAINT book_id IF NOT EXISTS FOR (b:Book) REQUIRE b.source_id IS UNIQUE",
                     "CREATE CONSTRAINT member_id IF NOT EXISTS FOR (m:Member) REQUIRE m.source_id IS UNIQUE",
                     "CREATE CONSTRAINT author_name IF NOT EXISTS FOR (a:Author) REQUIRE a.name IS UNIQUE",
                     "CREATE CONSTRAINT genre_name IF NOT EXISTS FOR (g:Genre) REQUIRE g.name IS UNIQUE"):
            s.run(stmt).consume()
        s.run("""
            UNWIND $rows AS r
            MERGE (b:Book {source_id: r.id})
              SET b.title = r.title, b.refreshed_at = datetime(), b.source_version = r.version
            MERGE (g:Genre {name: r.genre})
            MERGE (b)-[:IN_GENRE]->(g)
            WITH b, r
            UNWIND r.authors AS an
            MERGE (a:Author {name: an})
            MERGE (b)-[:BY]->(a)
        """, rows=[{"id": b["id"], "title": b["title"], "genre": b["genre"], "authors": b["authors"],
                    "version": b.get("version")} for b in books]).consume()
        s.run("""
            UNWIND $rows AS r
            MERGE (m:Member {source_id: r.id}) SET m.refreshed_at = datetime()
        """, rows=[{"id": m["id"]} for m in members]).consume()
        s.run("""
            UNWIND $rows AS r
            MATCH (m:Member {source_id: r.member_id}), (b:Book {source_id: r.book_id})
            MERGE (m)-[x:BORROWED]->(b) SET x.refreshed_at = datetime()
        """, rows=[{"member_id": l["member_id"], "book_id": l["book_id"]} for l in loans]).consume()


# --- incremental updates (called right after the PostgreSQL/MongoDB change) ---------------------
def upsert_book(driver, b):
    """Create or update one Book with its genre and authors (old genre/author links are replaced)."""
    with driver.session() as s:
        s.run("MATCH (:Book {source_id: $id})-[r:IN_GENRE|BY]->() DELETE r", id=b["id"]).consume()
        s.run("""
            MERGE (b:Book {source_id: $id}) SET b.title = $title, b.refreshed_at = datetime(),
                                                b.source_version = $version
            MERGE (g:Genre {name: $genre}) MERGE (b)-[:IN_GENRE]->(g)
            WITH b UNWIND $authors AS an
            MERGE (a:Author {name: an}) MERGE (b)-[:BY]->(a)
        """, id=b["id"], title=b["title"], genre=b["genre"], authors=b["authors"],
              version=b.get("version")).consume()
        _drop_orphans(s)


def delete_book(driver, book_id):
    with driver.session() as s:
        s.run("MATCH (b:Book {source_id: $id}) DETACH DELETE b", id=book_id).consume()
        _drop_orphans(s)


def _drop_orphans(s):
    s.run("MATCH (a:Author) WHERE NOT (a)<-[:BY]-() DELETE a").consume()
    s.run("MATCH (g:Genre) WHERE NOT (g)<-[:IN_GENRE]-() DELETE g").consume()


def record_loan(driver, member_id, book_id):
    with driver.session() as s:
        s.run("""
            MERGE (m:Member {source_id: $m}) ON CREATE SET m.refreshed_at = datetime()
            WITH m MATCH (b:Book {source_id: $b})
            MERGE (m)-[x:BORROWED]->(b) SET x.refreshed_at = datetime(), x.returned_at = null
        """, m=member_id, b=book_id).consume()


def record_return(driver, member_id, book_id):
    """The BORROWED edge stays (reading history feeds 'also borrowed'); it is marked returned."""
    with driver.session() as s:
        s.run("MATCH (:Member {source_id: $m})-[x:BORROWED]->(:Book {source_id: $b}) "
              "SET x.returned_at = datetime(), x.refreshed_at = datetime()", m=member_id, b=book_id).consume()


def also_borrowed(driver, book_id, limit=5):
    """Books borrowed by members who also borrowed `book_id`, most common first."""
    with driver.session() as s:
        return [dict(r) for r in s.run("""
            MATCH (:Book {source_id: $id})<-[:BORROWED]-(m:Member)-[:BORROWED]->(o:Book)
            WHERE o.source_id <> $id
            RETURN o.source_id AS book_id, count(DISTINCT m) AS members
            ORDER BY members DESC, book_id LIMIT $limit""", id=book_id, limit=limit)]


def same_author(driver, book_id, limit=5):
    with driver.session() as s:
        return [r["book_id"] for r in s.run("""
            MATCH (:Book {source_id: $id})-[:BY]->(:Author)<-[:BY]-(o:Book)
            WHERE o.source_id <> $id
            RETURN DISTINCT o.source_id AS book_id ORDER BY book_id LIMIT $limit""", id=book_id, limit=limit)]
