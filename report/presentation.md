---
title: ShelfSense – Project presentation II
---

# New since Presentation I

| Change | Before / after | Limit |
|---|---|---|
| Versioned rules + decision trail | v1 rejects S_FULL (limit 3); v2 (limit 4) allows it; old decision #1 still replays to *reject* | numeric parameters only |
| Hybrid search (FTS + vector, RRF) | MRR 0.821 -> 0.925, recall@5 0.73 -> 0.76 | regex labels favour keywords |
| Derived-copy registry + refresh | stale copies visible, only the changed book re-indexed | version check, not content |
| Safety + data checks | 34 safety tests, 21 data checks | one lock per book |

# 1. Why each store

| Store | Main query | Why |
|---|---|---|
| PostgreSQL | borrow in one transaction, `FOR UPDATE` on the book | constraints, atomic changes |
| MongoDB | `find_one` by id, `$jsonSchema` per type | different fields per book type |
| Redis | `GET decision:{m}:{b}`, TTL 300 s | cache, expires itself |
| Neo4j | `(:Book)<-[:BORROWED]-(:Member)-[:BORROWED]->(o)` | relationship pattern |
| Qdrant / pgvector | cosine top-k + filter | meaning search |

PostgreSQL alone: a self join on `loans` gives "also borrowed". Neo4j gives a shorter query but a copy that can drift.

# 2. The decision

Rules, first failing rule decides: unknown id -> **needs_info**, suspended, limit, duplicate, journal, no copy -> waitlist, else allow.
Concurrency: last copy, two members, **20/20** rounds exactly one allow.
Saved case: v1 reject (#1, `r2_limit`) -> v2 allow (#2, `r6_available`).

# 3. One source change

| State | Source | Stores | Search |
|---|---|---|---|
| before | v1 | v1, current | miss |
| stale | v2 | v1, STALE | miss |
| after refresh (0.3 s) | v2 | v2, current | hit |

# 4. Measured results (frozen labels, 20 queries, 15 loan cases)

| | recall@5 | MRR | ms |
|---|---|---|---|
| vector | 0.73 | 0.821 | 4.2 |
| keyword | 0.51 | 0.677 | 22.8 |
| hybrid | 0.76 | 0.925 | 29.1 |

pgvector vs Qdrant: top-10 overlap 1.0; ANN recall@10 at ef 64: 1.0 / 1.0; pgvector index 41 MB; at a 48.7% filter Qdrant needs ef>=64 (0.96).
Loans 15/15, safety 34/34, checks 21/21.
Wrong result: "coming-of-age adventure" – vector first hit rank 3, keyword/hybrid rank 1; caused by the **embedding stage** and label noise.

# 5. Explain one decision

`shelfsense explain 1`: member S_FULL, book free1, rule v1 (active now: v2), 3 active loans -> `r2_limit` -> reject. Replay reproduces the stored outcome.
