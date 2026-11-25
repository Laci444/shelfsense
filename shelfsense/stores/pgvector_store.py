"""pgvector store (derived): the same book embeddings as Qdrant, but inside PostgreSQL.

Table `book_vectors(book_id, embedding vector(384))` with an HNSW cosine index. Filters (genre, year) are
applied by joining `books`, so a filtered search sees the live source rows, not a copy of the metadata.
`<=>` is cosine distance; the score returned is 1 - distance (the same scale as Qdrant's cosine score).
"""
import io
import time

from shelfsense.config import EMBED_DIM


def _lit(vector):
    return "[" + ",".join(f"{float(x):.7f}" for x in vector) + "]"


def count(conn):
    return conn.execute("SELECT count(*) FROM book_vectors").fetchone()[0]


def rebuild(conn, ids, vectors, m=16, ef_construction=100):
    """Replace every vector. The HNSW index is created after the bulk load (much faster)."""
    t = time.time()
    with conn.transaction():
        conn.execute("DROP INDEX IF EXISTS book_vectors_hnsw")
        conn.execute("TRUNCATE book_vectors")
        with conn.cursor() as cur, cur.copy("COPY book_vectors (book_id, embedding) FROM STDIN") as cp:
            for i, v in zip(ids, vectors):
                cp.write_row((i, _lit(v)))
        conn.execute("SET LOCAL maintenance_work_mem = '512MB'")
        conn.execute("SET LOCAL max_parallel_maintenance_workers = 0")  # parallel build needs more /dev/shm than Docker gives
        conn.execute(f"CREATE INDEX book_vectors_hnsw ON book_vectors USING hnsw (embedding vector_cosine_ops) "
                     f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)})")
    return {"vectors": len(ids), "seconds": round(time.time() - t, 1)}


def index_size_bytes(conn):
    row = conn.execute("SELECT pg_relation_size('book_vectors_hnsw')").fetchone()
    return row[0]


def upsert_book(conn, book_id, vector):
    conn.execute("INSERT INTO book_vectors (book_id, embedding) VALUES (%s, %s::vector) "
                 "ON CONFLICT (book_id) DO UPDATE SET embedding = EXCLUDED.embedding, embedded_at = now()",
                 (book_id, _lit(vector)))


def delete_book(conn, book_id):
    conn.execute("DELETE FROM book_vectors WHERE book_id = %s", (book_id,))


def get(conn, book_id):
    row = conn.execute("SELECT embedding::text FROM book_vectors WHERE book_id = %s", (book_id,)).fetchone()
    return None if row is None else [float(x) for x in row[0].strip("[]").split(",")]


def search(conn, vector, limit=5, genre=None, year_from=None, ef_search=None, exact=False):
    """Top `limit` book ids by cosine similarity: [(book_id, score)].

    exact=True switches the index off (sequential scan) to get the true neighbours.
    A filtered ANN query uses hnsw.iterative_scan, so the index keeps scanning until enough rows pass the filter.
    """
    where, params = [], [_lit(vector)]
    if genre:
        where.append("b.genre = %s"); params.append(genre)
    if year_from is not None:
        where.append("b.year >= %s"); params.append(year_from)
    sql = ("SELECT v.book_id, 1 - (v.embedding <=> %s::vector) AS score FROM book_vectors v "
           + ("JOIN books b ON b.id = v.book_id " if where else "")
           + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "ORDER BY v.embedding <=> %s::vector LIMIT %s")
    params += [_lit(vector), limit]
    with conn.transaction():
        if exact:
            conn.execute("SET LOCAL enable_indexscan = off")
        else:
            if ef_search:
                conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
            conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = conn.execute(sql, params).fetchall()
    return [(r[0], float(r[1])) for r in rows]


assert EMBED_DIM == 384  # the column type in schema.sql
