---
geometry:
  - margin=2cm
---
# ShelfSense – report

## 1. Problem and decision
ShelfSense is a library loan advisor. For a (member, book) pair it returns **allow**, **waitlist** or **reject** with a reason,
and when the answer is not "allow" it suggests alternatives that have a free copy. One program uses five stores,
each for one reason. Data: 20,685 books from Open Library (CC0) plus synthetic members (3,006), loans (24,691) and
waitlist entries (401); see `data/raw/SOURCES.md`. The PDF suggests 200–1,000 records per store; I deliberately used more
so that Qdrant builds its HNSW index with default thresholds.

**Deviation from the PDF:** the PDF asks for pgvector and Qdrant to be compared head to head. I use Qdrant as the only vector store
(PostgreSQL is plain `postgres:16.4`), so the pgvector-specific items (shared ids between both systems, `ef_search`, iterative scan,
single-SQL join) are not done. The Qdrant + PostgreSQL join is done in two steps instead (section 5).

## 2. Architecture
| Store | Version | Role | Why |
|---|---|---|---|
| PostgreSQL | 16.4 | source of record | transactions and constraints for loans |
| MongoDB | 7.0.14 | source of record | book records that differ by type (`$jsonSchema` per type) |
| Redis | 7.2.5 | derived | decision cache, TTL 300 s; reading lists, TTL 1800 s |
| Neo4j | 5.24.2 | derived | graph: same author, "also borrowed" |
| Qdrant | 1.19.1 | derived | semantic search over descriptions (384-d MiniLM, cosine, HNSW) |

All stores start with `docker compose up` (pinned versions). The book id (Open Library work id) is the key in every store.
Derived stores keep `source_id` and `refreshed_at` (Redis decision entries too) and can be rebuilt from the sources with
`python -m shelfsense.sync` (`shelfsense db sync`) or kept in step by the Advisor.

**Decision rules** (first match wins): suspended -> at max loans -> already holds the book -> already on waitlist ->
journal for a standard member -> no copy (waitlist) -> allow.
**Write path** `borrow()`: one PostgreSQL transaction with `SELECT ... FOR UPDATE` on the book row; the loan, the availability
update and the decision row commit together. Afterwards cached decisions for that book are deleted in Redis.
**Return path** `return_book()`: one transaction that closes the loan, adds the copy back and walks the waitlist in order; the first member
who is allowed to borrow (the loan rules are re-evaluated) gets the copy and a decision "promoted from waitlist"; ineligible members
(e.g. suspended) stay in the queue.
Neo4j and Qdrant are updated right after the commit, one change at a time: `borrow()`/`return_book()` write or mark the `BORROWED` edge
(a waitlist promotion creates the promoted member's edge), and `add_book`/`update_book`/`delete_book` change PostgreSQL + MongoDB and then
the Neo4j node and the single Qdrant point (re-embedded only when title or description changed). If a derived store is down the source
change still stands (the error is collected in `Advisor.derived_errors`) and `sync` repairs it.

## 3. Test plan (written before tuning)
- 15 labelled loan decisions on fixed scenario members and books (`data/labels/loan_cases.json`).
- 20 free-text interest queries; a book is relevant if its genre is in the query's genre list and a regex fixed per query
  matches title, subjects or description (`data/build_labels.py`). The rules were not changed after seeing results.
- Metrics: recall@5 (hits divided by min(5, number of relevant books)), MRR, and ANN recall@10 against exact search.
- 46 automated tests (`pytest tests`, or `shelfsense test`) run against separate test databases, so they never change the real data. They cover the loan rules, return and waitlist promotion, concurrency, the data sources, propagation to Neo4j and Qdrant, the backend features of the TUI, and the TUI itself (headless).
- Every number below comes from one run of `shelfsense eval all`, saved in `report/results.txt` and `report/results.json`.

## 4. Results
**Loan decisions:** 15 of 15 labelled cases correct. **MongoDB validation:** an ebook without `size_mb` and a journal with
paperback fields are both rejected by `$jsonSchema`.

**Qdrant collection:** 20,685 points, `m=16`, `ef_construct=100`, default thresholds, 2 segments; **15,104** vectors are in the HNSW index
(the rest sit in a segment below the indexing threshold and are searched by scan). With the default of one segment per CPU the
upload was split into 8 small segments and `indexed_vectors` was 0, so the collection is created with `default_segment_number=1`.

**Retrieval quality** (20 queries): recall@5 = **0.73**, MRR = **0.846**.

**ANN vs exact search** (recall@10 of ANN against `exact=True`, 20 queries, median latency in ms, ANN / exact):

| hnsw_ef | no filter (20,685 rows) | `year>=2009` (908 rows, 4.4%) | `year>=1998` (10,005 rows, 48.4%) |
|---|---|---|---|
| 16 | 0.945 (3.32 / 3.98) | 1.0 (2.41 / 2.27) | 0.85 (2.63 / 3.78) |
| 64 | 0.995 (3.49 / 4.50) | 1.0 (2.31 / 2.40) | 0.99 (2.63 / 3.76) |
| 128 | 1.0 (3.10 / 3.84) | 1.0 (2.25 / 2.23) | 0.99 (3.57 / 4.43) |

Reading the table:
- A small `hnsw_ef` costs recall (0.945 unfiltered, 0.85 with the 48% filter); from 64 up, ANN finds nearly the same top-10 as exact search.
- The 4.4% filter keeps 908 rows, below `full_scan_threshold` (10,000 KB, roughly 6,500 vectors of 384 floats). Qdrant most likely answers it
  by exact scan, so its perfect recall and equal latency say nothing about HNSW. The 48% filter is above that threshold and is the real filtered-HNSW test:
  the filter lowers recall at small `hnsw_ef` (0.85 vs 0.945) and needs `hnsw_ef` >= 64 to recover.
- ANN is about 1.2–1.4x faster than exact search without a filter and with the 48% filter, but the absolute times are 2–4.5 ms and include
  the HTTP round trip from WSL to Docker, so I treat this as small and not as a proven speedup.
- All queries returned 10 results in every setting.

**Weak queries.** Query 20 has recall@5 = 0.2 and queries 10, 12 and 18 have 0.4.
Query 20 has 2,578 relevant books because its rule (`young|boy|girl|teen|adventure`) matches many unrelated descriptions
across three genres; the embedding captures meaning, the regex captures words, so part of the gap is label noise.
Another cause is the genre labels of the dump books, which are keyword heuristics.

## 5. Qdrant + PostgreSQL join (`eval/demo_join.py`)
The top-10 fantasy results from Qdrant (10 ids) are joined to PostgreSQL in one statement (`unnest(ids) WITH ORDINALITY`, which keeps the
similarity order) to read availability and active loans. All 10 had a free copy. Qdrant cannot see availability, so this join is what turns
"similar" into "similar and borrowable".

## 6. Update across stores (`eval/demo_update_across.py`)
The demo changes title and description of one book through the Advisor, borrows and returns it, then adds and deletes a book. Observed:

| step | Redis | Qdrant | Neo4j |
|---|---|---|---|
| `update_book` (title + description) | cached decision deleted | vector changed, title and `refreshed_at` updated (only this book re-embedded) | node title "Deep Sea Engineering" |
| `borrow` | – | – | `BORROWED` edge active |
| `return_book` | – | – | edge marked returned |
| `add_book` (2 copies) | – | new point | new Book node |
| `delete_book` | – | point removed | node removed |

PostgreSQL and MongoDB stay the sources of record; the derived stores follow within the same call, without a sync. The full `sync`
(about 10 minutes for 20k books on CPU) remains as a rebuild/repair job. The demo restores the data at the end.

## 7. Concurrency (`eval/demo_concurrency.py`)
Two members try to take the last copy at the same time (barrier-started threads, separate connections), 20 rounds:
in **20 of 20** rounds exactly one got `allow` and the other `waitlist`, and `available` ended at 0, never negative.
The row lock serialises the two transactions; the `CHECK (available >= 0)` constraint is a second safety net.

## 8. Failure case: stale cached decision (`eval/demo_stale_cache.py`)
`borrow()` invalidates Redis after the PostgreSQL commit. If the process dies between the two, or a copy is returned by another path,
Redis keeps the old answer. Demo: Redis says `waitlist (no copy available)`, PostgreSQL already says `allow`; the cached read still returns
`waitlist` with 300 s of TTL left. After explicit invalidation the next read returns `allow`.
Impact: bounded by the TTL (300 s), and only the advice is affected, because `borrow()` always re-checks in PostgreSQL under the lock,
so a stale entry cannot cause an oversold copy. Mitigations: shorter TTL, invalidate in the same code path as returns,
or write invalidation events via an outbox table (not implemented).

## 9. Limitations
- Genres of 20,248 dump books come from subject keywords; horror, fantasy and cooking have fewer books than other genres.
- Book type, copy counts, loans and type-specific Mongo fields are synthetic; page counts of dump books are random.
- Latency numbers include Docker/WSL network overhead and run on CPU embeddings; they are not production figures.
- Labels for retrieval are regex rules, an approximation of human relevance judgements.
- No pgvector comparison (see section 1); the 4.4% filter result does not exercise HNSW.

## 10. Application features
One `shelfsense` command (installed with `pip install -e .`, `pyproject.toml`) and a full-screen TUI (`shelfsense` without arguments).

| Feature | CLI | TUI | Stores involved |
|---|---|---|---|
| Loan decision + reason (allow / waitlist / reject) | `advise` | Advise tab: verdict banner and rule-by-rule trace | Redis cache, PostgreSQL |
| Alternatives with a free copy (same author, also borrowed, similar text) | `advise`, `similar` | Advise, Book tabs | Neo4j, Qdrant, PostgreSQL, MongoDB |
| Free-text recommendation, genre filter | `recommend` | Recommend tab | Qdrant, PostgreSQL, MongoDB |
| Borrow / waitlist (race-free) | `borrow` | Advise tab, Borrow button | PostgreSQL, Redis, Neo4j |
| Return with waitlist promotion; leave waitlist | `return` | Librarian and Member tabs | PostgreSQL, Redis, Neo4j |
| Book catalogue changes | `book add / edit / delete` | – | PostgreSQL, MongoDB, Neo4j, Qdrant, Redis |
| Search books and members | `search` | live search in the sidebar | PostgreSQL |
| Member profile, book details per store | – | Member and Book tabs | PostgreSQL, MongoDB, Neo4j, Redis |
| Store health, counts, cache viewer and flush, reset | `db status` | Stores tab | all five |
| Loan test cases | `eval all` | Scenarios tab (15/15) | PostgreSQL |
| Demos and evaluation | `eval vectors / join / concurrency / stale-cache / update-across / all` | Demos tab (live log) | all five |
| Data pipeline | `data build / fetch / generate / labels`, `db load`, `db sync` | – | – |

TUI keys: F1 help, F2–F9 tabs, Ctrl+T theme, Ctrl+E export of the last answer to `exports/`, Ctrl+R refresh, Ctrl+Q quit.
Demo scenarios with members, books and expected results are in `TEST_CASES.md` (38 cases and a 5-minute demo order).

## 11. How to reproduce
Data pipeline: `shelfsense data build --books N` streams the Open Library dumps into `data/raw/books.json`
(`fetch_books.py`), adds synthetic members, copies, loans and waitlist scaled to N into `data/generated/*.json` (`generate.py`)
and writes the labels (`generate.py` the loan cases, `build_labels.py` the retrieval queries); `load_sources` loads
PostgreSQL and MongoDB from those files (and rebuilds the Neo4j graph); `sync` builds Neo4j and Qdrant from the two databases (the slow, one-time step; afterwards changes are propagated incrementally). The JSON files are build outputs
and can be recreated at any time. The numbers in this report are from the 20,685-book snapshot.
```
shelfsense db up
shelfsense data build --books 20000
shelfsense db load && shelfsense db sync     # sync is the slow step, done once
shelfsense test                              # uses test databases, no re-sync needed
shelfsense eval all                          # writes report/results.txt and results.json
```
