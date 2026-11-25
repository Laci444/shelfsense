"""Full-text search (derived): PostgreSQL tsvector over each book's title and description.

The keyword half of hybrid search. A query is turned into an OR of its words (a long natural-language
question would match nothing with AND), ranked by ts_rank_cd. Genre/year filters join the live `books` table.
"""
import re

STOP = {"the", "and", "for", "with", "that", "from", "about", "their", "into", "his", "her", "its", "who", "are"}


def count(conn):
    return conn.execute("SELECT count(*) FROM book_fts").fetchone()[0]


def rebuild(conn, ids, texts):
    with conn.transaction():
        conn.execute("TRUNCATE book_fts")
        with conn.cursor() as cur, cur.copy("COPY book_fts (book_id, body) FROM STDIN") as cp:
            for i, t in zip(ids, texts):
                cp.write_row((i, t))
    return {"documents": len(ids)}


def upsert_book(conn, book_id, text):
    conn.execute("INSERT INTO book_fts (book_id, body) VALUES (%s, %s) "
                 "ON CONFLICT (book_id) DO UPDATE SET body = EXCLUDED.body", (book_id, text))


def delete_book(conn, book_id):
    conn.execute("DELETE FROM book_fts WHERE book_id = %s", (book_id,))


def tsquery(text):
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOP]
    return " | ".join(dict.fromkeys(words))


def search(conn, text, limit=5, genre=None, year_from=None):
    """[(book_id, score)] best first; empty when the query has no usable word."""
    q = tsquery(text)
    if not q:
        return []
    where, params = ["f.doc @@ to_tsquery('english', %s)"], [q, q]
    if genre:
        where.append("b.genre = %s"); params.append(genre)
    if year_from is not None:
        where.append("b.year >= %s"); params.append(year_from)
    params.append(limit)
    rows = conn.execute(
        "SELECT f.book_id, ts_rank_cd(f.doc, to_tsquery('english', %s)) AS score FROM book_fts f "
        "JOIN books b ON b.id = f.book_id WHERE " + " AND ".join(where) + " ORDER BY score DESC, f.book_id LIMIT %s",
        params).fetchall()
    return [(r[0], float(r[1])) for r in rows]
