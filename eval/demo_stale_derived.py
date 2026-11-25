"""Derived data going stale, then repaired: before / stale / after.

1. A temporary book is added with propagation ON: every store absorbs version 1.
2. Its description is changed with propagation OFF (a missed delivery). PostgreSQL/MongoDB are at version 2,
   the derived stores still describe version 1: a search for the new text misses, the registry shows 'stale'.
3. `refresh` re-indexes only the stale book. The search now hits and all versions and times match.
4. The temporary book is deleted (all stores, registry rows included).
Run: python -m eval.demo_stale_derived
"""
from shelfsense import decision, derived, retrieval
from shelfsense.stores import postgres, qdrant_store

BOOK_ID = "DEMO_STALE_BOOK"
OLD = "Honey bees, hives and pollination in spring gardens."
NEW = "Bathyscaphe engineering and deep sea pressure hulls."
QUERY = "bathyscaphe pressure hulls"


def _report(a, label, say):
    rows = {s: derived.entry(a.pg, s, BOOK_ID) for s in derived.STORES}
    src = a.pg.execute("SELECT version, updated_at FROM books WHERE id = %s", (BOOK_ID,)).fetchone()
    pts = a.qdrant.retrieve(qdrant_store.COLLECTION, [qdrant_store.point_id(BOOK_ID)])
    say(f"{label}: source {BOOK_ID} version {src[0]} (updated {str(src[1])[:19]})")
    for s, r in rows.items():
        state = "missing" if r is None else ("current" if r[0] == src[0] else "STALE")
        say(f"    {s:<9} version {r[0] if r else '-':<3} refreshed {str(r[1])[:19] if r else '-':<20} {state}")
    hit_v = [i for i, _ in retrieval.search(a.qdrant, a.pg, QUERY, mode="vector", limit=3)]
    hit_k = [i for i, _ in retrieval.search(a.qdrant, a.pg, QUERY, mode="keyword", limit=3)]
    say(f"    search '{QUERY}': vector {'HIT' if BOOK_ID in hit_v else 'miss'}, "
        f"keyword {'HIT' if BOOK_ID in hit_k else 'miss'}")
    return {"source_version": src[0], "stores": {s: (r[0] if r else None) for s, r in rows.items()},
            "vector_hit": BOOK_ID in hit_v, "keyword_hit": BOOK_ID in hit_k,
            "qdrant_payload_version": pts[0].payload.get("source_version") if pts else None}


def run(verbose=True):
    say = print if verbose else (lambda *a: None)
    a = decision.Advisor()
    a.propagate = True
    try:
        a.add_book({"id": BOOK_ID, "title": "Demo book", "authors": ["Demo Author"], "year": 2021,
                    "genre": "science", "description": OLD, "book_type": "paperback"})
        before = _report(a, "1. BEFORE (propagation on, in sync)", say)

        a.propagate = False  # the update reaches PostgreSQL/MongoDB only
        a.update_book(BOOK_ID, description=NEW)
        a.propagate = True
        stale = _report(a, "2. STALE (description changed, derived stores not told)", say)
        say(f"    pending refresh: { {s: len(derived.pending(a.pg, s)) for s in derived.STORES} }")

        result = a.refresh()
        say(f"3. refresh: {result['refreshed']} books re-indexed in {result['seconds']} s")
        after = _report(a, "   AFTER", say)
        return {"before": before, "stale": stale, "refresh": result, "after": after}
    finally:
        try:
            a.propagate = True
            a.delete_book(BOOK_ID)
            say("4. temporary book deleted from every store")
        except LookupError:
            pass
        a.close()


if __name__ == "__main__":
    run()
