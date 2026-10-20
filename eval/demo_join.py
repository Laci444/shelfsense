"""Demo: a Qdrant query joined with the business tables.

pgvector would do this in one SQL statement; with Qdrant the vector search is one call and the join with
PostgreSQL is one statement taking the ranked ids (unnest ... WITH ORDINALITY keeps the Qdrant order).
Run: python -m eval.demo_join
"""
from shelfsense import embeddings
from shelfsense.stores import postgres, qdrant_store

SQL = """
SELECT b.id, b.title, b.available, b.total,
       count(l.id) FILTER (WHERE l.returned_at IS NULL) AS active_loans,
       (SELECT count(*) FROM waitlist w WHERE w.book_id = b.id) AS waiting
FROM unnest(%s::text[]) WITH ORDINALITY AS q(id, rank)
JOIN books b ON b.id = q.id
LEFT JOIN loans l ON l.book_id = b.id
WHERE b.available > 0
GROUP BY b.id, q.rank
ORDER BY q.rank
"""


def run(text="epic fantasy with dragons and magic", genre="fantasy", k=10, verbose=True):
    client = qdrant_store.connect()
    hits = qdrant_store.search(client, embeddings.embed([text])[0], limit=k, genre=genre)
    with postgres.connect() as c:
        rows = c.execute(SQL, ([h[0] for h in hits],)).fetchall()
    out = {"query": text, "genre_filter": genre, "qdrant_top_k": len(hits), "free_after_join": len(rows),
           "rows": [dict(zip(("id", "title", "available", "total", "active_loans", "waiting"), r)) for r in rows]}
    if verbose:
        print(f"Qdrant top-{k} ({genre}) -> {len(hits)} ids; PostgreSQL join keeps {len(rows)} with a free copy:")
        for r in out["rows"]:
            print(f"  {r['id']:<12} {r['title'][:40]:<40} free={r['available']}/{r['total']} loans={r['active_loans']} waiting={r['waiting']}")
    return out


if __name__ == "__main__":
    run()
