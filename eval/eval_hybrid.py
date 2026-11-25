"""Vector vs keyword vs hybrid search on the 20 labelled interest queries.

Usage: python -m eval.eval_hybrid
Needs filled Qdrant and book_fts (`db sync`, or `db pgvector`, which also fills the full-text table).
Honest caveat: relevance in data/labels is rule-based (genre + keywords from the description), so keyword
search is partly measured with the same kind of signal it searches on. Treat the numbers as a comparison of
behaviours, not as absolute quality.
"""
import statistics

from eval.eval_vectors import QUERIES, ids
from shelfsense import embeddings, retrieval
from shelfsense.stores import postgres, qdrant_store


def _score(top, rel):
    recall = len([i for i in top if i in rel]) / min(5, len(rel))
    rr = next((1 / (r + 1) for r, i in enumerate(top) if i in rel), 0.0)
    return recall, rr


def run():
    q = qdrant_store.connect()
    pg = postgres.connect()
    pg.autocommit = True
    vecs = embeddings.embed([x["query"] for x in QUERIES])
    per, lat = {m: [] for m in retrieval.MODES}, {m: [] for m in retrieval.MODES}
    tops = {m: [] for m in retrieval.MODES}
    for qq, v in zip(QUERIES, vecs):
        rel = set(qq["relevant"])
        for m in retrieval.MODES:
            hits, ms = retrieval.timed_search(q, pg, qq["query"], mode=m, limit=5, vector=v)
            top = ids(hits)
            per[m].append(_score(top, rel)); lat[m].append(ms); tops[m].append(top)
    modes = {m: {"recall@5": round(statistics.mean(r for r, _ in per[m]), 3),
                 "MRR": round(statistics.mean(x for _, x in per[m]), 3),
                 "latency_ms": round(statistics.median(lat[m]), 1)} for m in retrieval.MODES}

    # the query where hybrid gains most over vector alone (or, failing that, where vector is weakest)
    gain = [(per["hybrid"][i][1] - per["vector"][i][1], i) for i in range(len(QUERIES))]
    best = max(gain)[1] if max(gain)[0] > 0 else min(range(len(QUERIES)), key=lambda i: per["vector"][i][1])
    qq = QUERIES[best]
    rel = set(qq["relevant"])
    titles = dict(pg.execute("SELECT id, title FROM books WHERE id = ANY(%s)",
                             ([i for m in retrieval.MODES for i in tops[m][best]],)).fetchall())

    def rank_of_first_relevant(m):
        full = ids(retrieval.search(q, pg, qq["query"], mode=m, limit=50, vector=vecs[best]))
        return next((r + 1 for r, i in enumerate(full) if i in rel), None)

    example = {"query": qq["query"], "rule": qq.get("rule"), "n_relevant": len(rel),
               "per_mode": {m: {"first_relevant_rank_in_top50": rank_of_first_relevant(m),
                                "top5": [{"id": i, "title": titles.get(i), "relevant": i in rel}
                                         for i in tops[m][best]]} for m in retrieval.MODES}}
    return {"n_queries": len(QUERIES), "modes": modes, "example": example}


def main():
    out = run()
    print(f"{out['n_queries']} labelled queries; K={retrieval.K} reciprocal rank fusion\n")
    print(f"{'mode':<9}{'recall@5':>10}{'MRR':>8}{'median ms':>11}")
    for m, r in out["modes"].items():
        print(f"{m:<9}{r['recall@5']:>10}{r['MRR']:>8}{r['latency_ms']:>11}")
    ex = out["example"]
    print(f"\nExplained case: {ex['query']!r} ({ex['n_relevant']} relevant books, rule: {ex['rule']})")
    for m, d in ex["per_mode"].items():
        print(f"  {m}: first relevant at rank {d['first_relevant_rank_in_top50']}")
        for r in d["top5"]:
            print(f"     {'*' if r['relevant'] else ' '} {r['id']}  {(r['title'] or '')[:70]}")
    print("  (* = labelled relevant)")
    return out


if __name__ == "__main__":
    main()
