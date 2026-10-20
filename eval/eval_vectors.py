"""Retrieval quality and ANN measurements for Qdrant. Every number is printed by this script.

Usage: python -m eval.eval_vectors [--json out.json]

1. recall@5 and MRR on the 20 labelled interest queries (data/labels/interest_queries.json).
   recall@5 = relevant books in top 5 / min(5, #relevant)  (capped, so 1.0 is reachable
   for queries with many relevant books); MRR = mean 1/rank of the first relevant book.
2. ANN recall@10: HNSW top-10 vs exact top-10 (same collection, exact=True), 20 fixed queries.
3. Filtered search keeping ~5% of the rows (payload `year` >= threshold): results returned,
   ANN recall@10 against the exact filtered top-10, latency.
"""
import json
import statistics
import sys
import time
from pathlib import Path

from shelfsense import embeddings
from shelfsense.config import COLLECTION, DATA_DIR
from shelfsense.stores import mongo, qdrant_store
from qdrant_client import models

ALL_QUERIES = json.loads((DATA_DIR / "labels" / "interest_queries.json").read_text(encoding="utf-8"))
QUERIES = [q for q in ALL_QUERIES if q["relevant"]]  # a query nobody can answer in a small dataset is skipped
HNSW_EF_VALUES = [16, 64, 128]
REPEATS = 5


def ids(hits):
    return [h[0] for h in hits]


def timed(fn):
    best = []
    for _ in range(REPEATS):
        t = time.perf_counter()
        res = fn()
        best.append((time.perf_counter() - t) * 1000)
    return res, statistics.median(best)


def retrieval_quality(client, vecs):
    recalls, rrs, per_query = [], [], []
    for q, v in zip(QUERIES, vecs):
        rel = set(q["relevant"])
        top = ids(qdrant_store.search(client, v, limit=5, hnsw_ef=128))
        hit = [i for i in top if i in rel]
        recall = len(hit) / min(5, len(rel))
        rr = next((1 / (r + 1) for r, i in enumerate(top) if i in rel), 0.0)
        recalls.append(recall); rrs.append(rr)
        per_query.append({"id": q["id"], "relevant": len(rel), "recall@5": round(recall, 2), "rr": round(rr, 2)})
    return {"recall@5": round(statistics.mean(recalls), 3), "MRR": round(statistics.mean(rrs), 3),
            "per_query": per_query}


def ann_recall(client, vecs, ef, **flt):
    rec, lat, counts, exact_lat = [], [], [], []
    for v in vecs:
        exact, el = timed(lambda: qdrant_store.search(client, v, limit=10, exact=True, **flt))
        ann, al = timed(lambda: qdrant_store.search(client, v, limit=10, hnsw_ef=ef, **flt))
        denom = len(exact) or 1
        rec.append(len(set(ids(exact)) & set(ids(ann))) / denom)
        counts.append(len(ann)); lat.append(al); exact_lat.append(el)
    return {"hnsw_ef": ef, "ann_recall@10": round(statistics.mean(rec), 3),
            "results_returned_avg": round(statistics.mean(counts), 2), "results_returned_min": min(counts),
            "latency_ms_median": round(statistics.median(lat), 2),
            "exact_latency_ms_median": round(statistics.median(exact_lat), 2)}


def year_threshold_for_share(client, share):
    years = sorted(p.payload["year"] for p in client.scroll(COLLECTION, limit=client.count(COLLECTION).count, with_payload=["year"])[0])
    n = len(years)
    best = min(set(years), key=lambda y: abs(sum(1 for x in years if x >= y) / n - share))
    kept = sum(1 for x in years if x >= best)
    return best, kept, n


def filtered_block(client, vecs, share):
    y, kept, n = year_threshold_for_share(client, share)
    return {"filter": f"year >= {y}", "rows_kept": kept, "rows_total": n, "share": round(kept / n, 3),
            "runs": [ann_recall(client, vecs, ef, year_from=y) for ef in HNSW_EF_VALUES]}


def run():
    client = qdrant_store.connect()
    info = client.get_collection(COLLECTION)
    vecs = embeddings.embed([q["query"] for q in QUERIES])
    out = {"collection": {"points": info.points_count, "indexed_vectors": info.indexed_vectors_count,
                          "segments": info.segments_count,
                          "hnsw": info.config.hnsw_config.model_dump(),
                          "payload_indexes": list(info.payload_schema)},
           "model": "all-MiniLM-L6-v2 (normalised, cosine)", "n_queries": len(QUERIES),
           "n_queries_skipped_no_relevant": len(ALL_QUERIES) - len(QUERIES)}
    out["retrieval"] = retrieval_quality(client, vecs)
    out["ann_unfiltered"] = [ann_recall(client, vecs, ef) for ef in HNSW_EF_VALUES]
    out["filter_5pct"] = filtered_block(client, vecs, 0.05)
    out["filter_50pct"] = filtered_block(client, vecs, 0.50)
    return out


def main():
    out = run()
    text = json.dumps(out, indent=1)
    print(text)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(text)


if __name__ == "__main__":
    main()
