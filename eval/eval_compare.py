"""pgvector vs Qdrant on the same vectors, the same queries and the same labels.

Usage: python -m eval.eval_compare [--json out.json]
Needs `db sync` (or `db pgvector`) so that both stores hold the same books.

Per store: retrieval quality on the labelled interest queries, ANN recall@10 against the exact top-10 of the
same store, filtered search (year >= threshold keeping ~5% and ~50% of the rows), median latency, and the
overlap between the two stores' top-10 lists. Case counts are part of the output.
"""
import json
import statistics
import sys
from pathlib import Path

from eval.eval_vectors import HNSW_EF_VALUES, QUERIES, ids, timed, year_threshold_for_share
from shelfsense import embeddings
from shelfsense.stores import pgvector_store, postgres, qdrant_store


def quality(search, vecs):
    recalls, rrs = [], []
    for q, v in zip(QUERIES, vecs):
        rel = set(q["relevant"])
        top = ids(search(v, 5, 128))
        recalls.append(len([i for i in top if i in rel]) / min(5, len(rel)))
        rrs.append(next((1 / (r + 1) for r, i in enumerate(top) if i in rel), 0.0))
    return {"recall@5": round(statistics.mean(recalls), 3), "MRR": round(statistics.mean(rrs), 3)}


def ann(search, vecs, ef, **flt):
    rec, lat, cnt, elat = [], [], [], []
    for v in vecs:
        exact, el = timed(lambda: search(v, 10, None, exact=True, **flt))
        got, al = timed(lambda: search(v, 10, ef, **flt))
        rec.append(len(set(ids(exact)) & set(ids(got))) / (len(exact) or 1))
        cnt.append(len(got)); lat.append(al); elat.append(el)
    return {"ef": ef, "ann_recall@10": round(statistics.mean(rec), 3), "results_min": min(cnt),
            "latency_ms": round(statistics.median(lat), 2), "exact_latency_ms": round(statistics.median(elat), 2)}


def run():
    q = qdrant_store.connect()
    pg = postgres.connect()
    pg.autocommit = True
    n_q, n_p = q.count(qdrant_store.COLLECTION).count, pgvector_store.count(pg)
    vecs = embeddings.embed([x["query"] for x in QUERIES])

    stores = {
        "qdrant": lambda v, k, ef, exact=False, **f: qdrant_store.search(q, v, limit=k, hnsw_ef=ef, exact=exact, **f),
        "pgvector": lambda v, k, ef, exact=False, **f: pgvector_store.search(pg, v, limit=k, ef_search=ef,
                                                                              exact=exact, **f)}
    year, kept5, total = year_threshold_for_share(q, 0.05)
    year50, kept50, _ = year_threshold_for_share(q, 0.50)
    out = {"n_queries": len(QUERIES), "vectors": {"qdrant": n_q, "pgvector": n_p},
           "filters": {"5pct": {"year>=": year, "rows": kept5}, "50pct": {"year>=": year50, "rows": kept50},
                       "rows_total": total},
           "pgvector_index_mb": round(pgvector_store.index_size_bytes(pg) / 1e6, 1)}
    for name, search in stores.items():
        out[name] = {"quality": quality(search, vecs),
                     "ann_unfiltered": [ann(search, vecs, ef) for ef in HNSW_EF_VALUES],
                     "filter_5pct": [ann(search, vecs, ef, year_from=year) for ef in HNSW_EF_VALUES],
                     "filter_50pct": [ann(search, vecs, ef, year_from=year50) for ef in HNSW_EF_VALUES]}
    overlap = [len(set(ids(stores["qdrant"](v, 10, 128))) & set(ids(stores["pgvector"](v, 10, 128)))) / 10
               for v in vecs]
    out["top10_overlap_between_stores"] = round(statistics.mean(overlap), 3)
    pg.close()
    return out


def main():
    out = run()
    text = json.dumps(out, indent=1)
    print(text)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(text)


if __name__ == "__main__":
    main()
