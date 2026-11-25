"""Run every measurement and demo; write report/results.json and report/results.txt.

Every number in the report comes from this output. Usage: python -m eval.run_all
Needs the stores filled (load_sources + sync). Read-only checks run first; the demos reset what they change.
"""
import json
import platform
import time
from pathlib import Path

from pymongo.errors import WriteError

from eval import (demo_concurrency, demo_join, demo_rules, demo_stale_cache, demo_stale_derived,
                  demo_update_across, eval_compare, eval_hybrid, eval_vectors)
from shelfsense import check, labels
from shelfsense.config import DATA_DIR
from shelfsense.stores import mongo, neo4j_graph, postgres, qdrant_store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "report"


def safety_tests():
    """Run the safety / duplicate tests (they use the test databases) and keep the pass count."""
    import re
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_safety.py"], cwd=ROOT,
                       capture_output=True, text=True)
    m = re.search(r"(\d+) passed", p.stdout)
    failed = re.search(r"(\d+) failed", p.stdout)
    return {"passed": int(m.group(1)) if m else 0, "failed": int(failed.group(1)) if failed else 0}


def data_checks():
    rows = check.run()
    return {"checks": len(rows), "passed": sum(1 for _, ok, _ in rows if ok),
            "failed": [{"check": n, "detail": d} for n, ok, d in rows if not ok]}


def store_counts():
    with postgres.connect() as c:
        pg = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("members", "books", "loans", "waitlist")}
    drv = neo4j_graph.connect()
    with drv.session() as s:
        neo = {"books": s.run("MATCH (b:Book) RETURN count(b) AS n").single()["n"],
               "borrowed_edges": s.run("MATCH ()-[x:BORROWED]->() RETURN count(x) AS n").single()["n"]}
    drv.close()
    q = qdrant_store.connect()
    return {"postgres": pg, "mongo_books": mongo.connect()["books"].count_documents({}), "neo4j": neo,
            "qdrant_points": q.count(qdrant_store.COLLECTION).count}


def loan_cases():
    cases = json.loads((DATA_DIR / "labels" / "loan_cases.json").read_text())
    rows = []
    with postgres.connect() as c:
        for case in cases:
            got, reason = postgres.check(c, case["member"], case["book"])
            rows.append({"id": case["id"], "expected": case["expected"], "got": got, "ok": got == case["expected"]})
    return {"cases": len(rows), "correct": sum(r["ok"] for r in rows), "failed": [r for r in rows if not r["ok"]]}


def mongo_validation():
    coll = mongo.connect()["books"]
    bad = [("ebook without size_mb", {"book_type": "ebook", "file_format": "epub"}),
           ("journal with paperback fields", {"book_type": "journal", "pages": 100, "isbn": "1"})]
    res = {}
    for i, (name, extra) in enumerate(bad):
        doc = {"_id": f"BAD{i}", "title": "t", "authors": ["a"], "year": 2000, "genre": "fantasy",
               "description": "d", **extra}
        try:
            coll.insert_one(doc)
            coll.delete_one({"_id": doc["_id"]})
            res[name] = "ACCEPTED (unexpected)"
        except WriteError:
            res[name] = "rejected by $jsonSchema"
    return res


def text_summary(r):
    v = r["vectors"]
    lines = [f"ShelfSense results  ({r['started']}, python {r['python']})", "",
             f"Store counts: {json.dumps(r['store_counts'])}",
             f"Qdrant: {v['collection']['points']} points, {v['collection']['indexed_vectors']} indexed, "
             f"{v['collection']['segments']} segments, hnsw m={v['collection']['hnsw']['m']} "
             f"ef_construct={v['collection']['hnsw']['ef_construct']} "
             f"full_scan_threshold={v['collection']['hnsw']['full_scan_threshold']} KB",
             f"Loan cases: {r['loan_cases']['correct']}/{r['loan_cases']['cases']} correct",
             f"Mongo validation: {json.dumps(r['mongo_validation'])}", "",
             f"Retrieval ({v['n_queries']} queries): recall@5={v['retrieval']['recall@5']}  MRR={v['retrieval']['MRR']}",
             "per query (id: relevant, recall@5): " + ", ".join(
                 f"{q['id']}:{q['relevant']},{q['recall@5']}" for q in v["retrieval"]["per_query"]), "",
             "ANN vs exact (recall@10, latency ms ANN / exact):"]
    blocks = [("no filter", {"rows_kept": v["collection"]["points"], "share": 1.0, "runs": v["ann_unfiltered"]}),
              (f"filter {v['filter_5pct']['filter']}", v["filter_5pct"]),
              (f"filter {v['filter_50pct']['filter']}", v["filter_50pct"])]
    for name, b in blocks:
        lines.append(f"  {name}: {b['rows_kept']} rows ({b['share'] * 100:.1f}%)")
        for run in b["runs"]:
            lines.append(f"    hnsw_ef={run['hnsw_ef']:<4} recall@10={run['ann_recall@10']}  "
                         f"returned avg/min={run['results_returned_avg']}/{run['results_returned_min']}  "
                         f"{run['latency_ms_median']} / {run['exact_latency_ms_median']}")
    c = r["concurrency"]
    lines += ["", f"Concurrency: {c['rounds_correct']}/{c['rounds']} rounds exactly one allow + one waitlist",
              "", "Qdrant + PostgreSQL join: top-{} -> {} ids -> {} with a free copy".format(
                  10, r["join"]["qdrant_top_k"], r["join"]["free_after_join"]),
              "", "Stale cache: " + json.dumps(r["stale_cache"]),
              "", "Update across stores: " + json.dumps(r["update_across"])]
    lines += ["", f"Labels frozen: {r['labels']['frozen']} (since {r['labels']['frozen_on']}), "
                  f"unchanged: {r['labels']['ok']}, counts {json.dumps(r['labels']['counts'])}",
              "", f"Hybrid search ({r['hybrid']['n_queries']} queries):"]
    for m, x in r["hybrid"]["modes"].items():
        lines.append(f"  {m:<8} recall@5={x['recall@5']}  MRR={x['MRR']}  {x['latency_ms']} ms")
    ex = r["hybrid"]["example"]
    lines.append(f"  explained query: {ex['query']!r}, first relevant rank per mode: "
                 + json.dumps({m: d['first_relevant_rank_in_top50'] for m, d in ex['per_mode'].items()}))
    cp = r["compare"]
    lines += ["", f"pgvector vs Qdrant ({cp['n_queries']} queries, {cp['vectors']}): top-10 overlap "
                  f"{cp['top10_overlap_between_stores']}, pgvector index {cp['pgvector_index_mb']} MB"]
    for s in ("qdrant", "pgvector"):
        lines.append(f"  {s}: quality {json.dumps(cp[s]['quality'])}")
        for key in ("ann_unfiltered", "filter_5pct", "filter_50pct"):
            lines.append(f"    {key}: " + "; ".join(
                f"ef={x['ef']} recall@10={x['ann_recall@10']} {x['latency_ms']}/{x['exact_latency_ms']} ms"
                for x in cp[s][key]))
    lines += ["", "Rules demo: " + json.dumps(r["rules_demo"]),
              "", "Stale derived data: " + json.dumps(r["stale_derived"], default=str),
              "", f"Safety tests: {json.dumps(r['safety_tests'])}",
              f"Data checks: {r['data_checks']['passed']}/{r['data_checks']['checks']} passed "
              + json.dumps(r["data_checks"]["failed"])]
    return "\n".join(lines)


def main():
    t0 = time.time()
    r = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "python": platform.python_version()}
    r["store_counts"] = store_counts()
    r["loan_cases"] = loan_cases()
    r["mongo_validation"] = mongo_validation()
    r["vectors"] = eval_vectors.run()
    r["join"] = demo_join.run(verbose=False)
    r["concurrency"] = demo_concurrency.run(verbose=False)
    r["stale_cache"] = demo_stale_cache.run(verbose=False)
    r["update_across"] = demo_update_across.run(verbose=False)
    r["labels"] = labels.verify()
    r["hybrid"] = eval_hybrid.run()
    r["compare"] = eval_compare.run()
    r["rules_demo"] = demo_rules.run(verbose=False)
    r["stale_derived"] = demo_stale_derived.run(verbose=False)
    r["safety_tests"] = safety_tests()
    from shelfsense import sync
    sync.sync_graph()  # the demos above reload PostgreSQL; bring the graph back in step before the checks
    r["data_checks"] = data_checks()
    r["seconds"] = round(time.time() - t0, 1)
    OUT.mkdir(exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    text = text_summary(r)
    (OUT / "results.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
