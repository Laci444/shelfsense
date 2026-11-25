---
geometry:
  - margin=1.8cm
fontsize: 10pt
---
# ShelfSense – report (Project presentation II)

ShelfSense is a library loan advisor. For a (member, book) pair it returns **allow**, **waitlist**, **reject** or **needs_info**,
the reason, and alternatives that have a free copy. Data: 20,000 Open Library books (CC0, `data/raw/SOURCES.md`) plus synthetic
members (2,906), loans (24,512) and waitlist entries (401). Stores: PostgreSQL 16 + pgvector 0.8, MongoDB 7, Redis 7, Neo4j 5,
Qdrant 1.19. The book id (Open Library work id) is the key in every store. PostgreSQL and MongoDB are the sources of record;
Redis, Neo4j, Qdrant, pgvector and the full-text table are **derived copies**.
Every number below is printed by `shelfsense eval all` (`report/results.txt`, `results.json`).

## 0. What is new since Project presentation I
| # | Change (lecture) | Before / after | Still not handled |
|---|---|---|---|
| 1 | **Versioned rules** (11): rules live in table `rules` with validity dates; every decision stores `rule_version`, `rule_id` and its `inputs` | Rules v1 reject `S_FULL` (limit 3). After publishing v2 (limit 4) the same request is allowed; the old decision #1 is replayed with v1 and still gives *reject* (`replay_ok`) | Rules are numeric parameters, not new rule logic; a new kind of rule needs code |
| 2 | **Missing evidence -> `needs_info`** (11): an unknown member or book is no longer an error | Before: exception. After: stored decision *needs_info*; 24 hostile-id cases (SQL/Cypher injection, empty, NUL, very long) change no rows | A misspelled but valid id is a different member; no fuzzy matching |
| 3 | **Hybrid search** (11): keyword (PostgreSQL full text) + vector (Qdrant), reciprocal rank fusion K=60 | recall@5 0.73 -> 0.76, MRR 0.821 -> 0.925 on 20 labelled queries (section 4) | Labels are regex rules, so keyword search is partly measured with its own kind of signal |
| 4 | **Derived-copy registry + refresh** (9): `derived_state` stores the source version each copy was built from | A missed update is visible as *stale* and `refresh` repairs only that book (demo in section 3) | Detects staleness by version, not by comparing content; Redis is not in the registry (TTL only) |
| 5 | **Concurrency and safety tests** (8) and **data checks** (10) | 34 safety tests, 21 data checks (`db check`); a double borrow by one member, from 4 threads, gives exactly one *allow* | One database lock per book; no distributed locking across several app servers |
| 6 | **pgvector next to Qdrant** (lecture on vector stores) | Same top-10 for both (overlap 1.0), different speed and filter behaviour (section 4) | One embedding model; no hybrid inside pgvector |

## 1. Why each store holds its data
| Store | Main query (from the code) | Why here |
|---|---|---|
| PostgreSQL | `SELECT ... FROM books WHERE id=$1 FOR UPDATE`, then insert loan, `available-1`, insert decision, one transaction | several rows change together; `CHECK (available>=0)` and the unique partial index `uq_active_loan(member_id, book_id) WHERE returned_at IS NULL` are checked on every write |
| MongoDB | `books.insert_one(doc)` / `find_one({_id})` with a `$jsonSchema` per `book_type` | an ebook has `size_mb`, a journal has `issn`; one document is read and written as a whole. An ebook without `size_mb` and a journal with paperback fields are rejected |
| Redis | `GET decision:{member}:{book}` (TTL 300 s) | read by key, expires on its own; the answer is only a cache, the truth stays in PostgreSQL |
| Neo4j | `MATCH (:Book {source_id:$id})<-[:BORROWED]-(:Member)-[:BORROWED]->(o:Book) RETURN o, count(*) ORDER BY 2 DESC` | "also borrowed" and "same author" are relationship patterns |
| Qdrant, pgvector | cosine top-k over 384-d MiniLM vectors with a `genre` / `year` filter | finds a book by meaning; exact fields (year, genre) are only filters |
| PostgreSQL full text | `to_tsvector('english', body) @@ to_tsquery(...)`, `ts_rank_cd` | exact words (names, codes) that embeddings blur |

**PostgreSQL alone would also work** for "also borrowed": a self join on `loans` (`l1 JOIN l2 ON l1.member_id=l2.member_id AND l1.book_id<>l2.book_id`).
Neo4j gives a one-line pattern and a second copy that can drift (24,506 edges, which `db check` compares with the 24,506 distinct member-book pairs
in PostgreSQL). For a fixed two-step join this is not needed; I kept it because the graph also answers same-author and, later, longer paths.
Qdrant and pgvector hold the same vectors on purpose: the comparison in section 4 is a handout task.

## 2. The decision
**Method: rules** (first failing rule decides), parameters from the active row in `rules`. In order: unknown member/book -> `needs_info` (r0);
suspended (r1); at the loan limit (r2_limit); already holds the book; already on the waitlist; journal for a standard member;
no copy -> waitlist; otherwise allow (r6_available). Code: `rules.evaluate` (pure function of `inputs` and `params`), `postgres.borrow`.
`borrow` runs in one transaction with a row lock on the book; the loan, the new availability and the decision row commit together,
then Redis is invalidated and Neo4j, Qdrant, pgvector and full text follow for that one book.

Saved case (`eval rules`): *rules v1: reject - member at max loans (decision #1)*; *published v2: same request -> allow - copy available (decision #2)*.
**Concurrency test** (`eval concurrency`): two members take the last copy at the same moment, 20 rounds: exactly one *allow* and one *waitlist* in **20/20** rounds,
`available` never below 0. **Safety tests** (`tests/test_safety.py`, 34 tests, all pass): 24 hostile ids (12 strings as member and as book) give `needs_info`
and unchanged row counts, 3 unknown-id cases are stored, a suspended member is rejected, borrowing twice gives one loan, waiting three times one waitlist row,
4 threads borrowing for the same member give one *allow*, returning twice or without a loan raises, a duplicate book id is refused, and the data checks catch deliberate corruption.

## 3. One source change and its derived copies (`eval stale`)
A temporary book `DEMO_STALE_BOOK` is added with propagation on. Its description is then changed while propagation is off (a missed delivery).

| State | Source (PostgreSQL) | neo4j / qdrant / pgvector / fts | search "bathyscaphe pressure hulls" |
|---|---|---|---|
| 1. before | version 1 | 1 / 1 / 1 / 1, all *current* | vector miss, keyword miss (text not there yet) |
| 2. stale | version 2 | 1 / 1 / 1 / 1, all **STALE** (4 pending) | vector miss, keyword miss |
| 3. after `refresh` | version 2 | 2 / 2 / 2 / 2, all current (0.3 s, only this book) | vector **hit**, keyword **hit** |

The run prints source ids, versions and refresh times per store. The trigger on `books` raises `version` only when title, genre, year or type change (not on
`available`), and a Mongo-only description change touches `updated_at`. In the full `eval all` run, refresh also repaired one other book whose version
the previous demo's data reload had reset, which shows the registry catches this as well. The registry at the end: 20,000 current rows per store, 0 stale, 0 missing, 0 orphans.

## 4. Measured results
Labels and test cases were written before tuning and are **frozen** (`data/labels/labels.lock.json`, SHA-256 of 20 interest queries and 15 loan cases; `eval labels`).
**Loan decisions:** 15/15 correct. **Retrieval:** recall@5 = hits / min(5, #relevant), MRR = mean 1/rank of the first relevant book, 20 queries.

| mode | recall@5 | MRR | median latency |
|---|---|---|---|
| vector (Qdrant) | 0.73 | 0.821 | 4.2 ms |
| keyword (PostgreSQL FTS) | 0.51 | 0.677 | 22.8 ms |
| hybrid (RRF, K=60) | **0.76** | **0.925** | 29.1 ms |

**pgvector vs Qdrant** (same 20 queries, 20,000 vectors each; recall@10 of ANN against exact search; median ms ANN / exact; filters keep 3.9% (`year>=2009`) and 48.7% (`year>=1998`) of the rows):

| setting | Qdrant | pgvector |
|---|---|---|
| quality (recall@5 / MRR) | 0.73 / 0.821 | 0.73 / 0.821 (top-10 overlap 1.0) |
| no filter, ef 16 / 64 / 128 | 0.985 / 1.0 / 1.0, 2.7-3.0 vs 3.5 ms | 0.925 / 1.0 / 1.0, 1.3-2.2 vs 8.7-9.0 ms |
| 3.9% filter, ef 64 | 1.0, 2.2 vs 2.2 ms | 0.985, 3.8 vs 3.8 ms |
| 48.7% filter, ef 16 / 64 / 128 | 0.865 / 0.96 / 0.99, 2.8-3.4 vs 3.8-4.1 ms | 0.955 / 0.995 / 0.995, 1.6-2.7 vs 9.2-9.7 ms |
| index size | HNSW m=16, ef_construct=100 | 41.0 MB, same parameters |

Reading: both stores give identical answers; pgvector's ANN is much faster than its exact scan (it scans 20,000 rows) while Qdrant's exact search is already fast.
With the selective filter (788 rows, below `full_scan_threshold`) both effectively scan, so that row says nothing about HNSW. With the 48.7% filter Qdrant needs `ef >= 64` for recall 0.96, and pgvector with iterative scan stays above 0.95.
Latencies include the Docker/WSL round trip and a CPU embedding model; they are not production figures. A **second** measure: concurrency 20/20, safety tests 34/34, data checks 21/21.

**One wrong result explained.** Query "coming-of-age adventure for young readers" (label rule `young|boy|girl|teen|adventure`, many relevant books): the first relevant book is at rank 3 for
vector search but rank 1 for keyword and hybrid search. The stage that caused it is **retrieval by meaning**: the embedding ranks books about growing up above books that contain the
words of the label. Part of the gap is label noise (the label is a word match, the embedding a meaning match); weak queries 5, 6, 10, 18 and 20 (recall@5 = 0.4) have the same cause.
Genres of the dump books are keyword heuristics, which adds noise.

## 5. Explaining one decision later
Every decision row holds the time, outcome, reason, `rule_version`, `rule_id` and `inputs` (JSON with the member id/version/tier/suspended, book id/version/type/available,
active loan count, holds/waitlist flags). `shelfsense explain <id>` (and the TUI Librarian tab, Enter on a decision) re-evaluates the *stored* inputs with the *stored* rule version, not the active one.
Example from the saved run: **decision #1** — member `S_FULL`, book `free1`, rule version **1**. The stored inputs show 3 active loans, tier *standard*; rules v1 allow 3 loans,
so rule `r2_limit` returned *reject*. After v2 (limit 4) was published, `explain 1` still reports v1 (active now: v2) and the replay gives *reject* (`replay_ok = true`); decision #2 was made under v2 and replays to *allow* (`r6_available`).

## 6. Limitations and honesty notes
- Retrieval labels are regex rules: an approximation of human judgement. Keyword and hybrid search may profit from this.
- Book type, copies, loans and Mongo type fields are synthetic; 20,000 books is larger than the handout's 200-1,000, so HNSW thresholds apply.
- 37 pairs of books share title and year (distinct Open Library works); `db check` reports them as a warning.
- Tests use separate PostgreSQL, MongoDB and Redis databases but the real Neo4j and Qdrant with throw-away ids; the suite removes the leftovers.

## 7. Reproduce
```
shelfsense db up && shelfsense data build --books 20000
shelfsense db load && shelfsense db sync        # sync is the slow one-time step (also fills pgvector and full text)
shelfsense db derived --adopt                   # mark all copies current once, after a fresh sync
shelfsense test                                 # 108 tests
shelfsense eval all                             # report/results.txt and results.json
shelfsense db check                             # 21 data checks
```
