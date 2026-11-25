# ShelfSense - user test cases for the demo

Every case is a command, the member and book it uses, and the expected result. The ids are the scenario members and
books of the current 20,000-book dataset (`data/generated/scenario.json`; a rebuilt dataset has other book ids).
The expected outcomes follow from the loan rules, not from running the program, and `shelfsense eval all` checks the 15 loan cases automatically.

Start from a clean state before the demo and after anything that changes data (`borrow`, the `eval` demos):
```
shelfsense db load        # resets PostgreSQL + MongoDB and flushes the Redis cache (fast; no sync needed)
```

## Test members
| Member | Tier | Max loans | State |
|---|---|---|---|
| `S_STD` | standard | 3 | no loans, in good standing |
| `S_PREM` | premium | 6 | no loans |
| `S_SUSP` | standard | 3 | suspended |
| `S_FULL` | standard | 3 | already has 3 active loans (the maximum) |
| `S_HAS` | standard | 3 | holds *Nightfall* |
| `S_WAIT` | standard | 3 | on the waitlist for *Roadside Picnic* |

## Test books
| Id | Title | Type | Free / total copies | Use |
|---|---|---|---|---|
| `OL17610507W` | Death's End | paperback | 3 / 3 | normal available book |
| `OL46401W` | The Caves of Steel | paperback | 3 / 3 | normal available book (similar-books demo) |
| `OL1963251W` | The Fall of Hyperion | paperback | 1 / 2 | one copy left |
| `OL46398W` | The Naked Sun | paperback | 0 / 2 | all copies on loan |
| `OL7967812W` | Roadside Picnic | paperback | 0 / 2 | all copies on loan, `S_WAIT` waits for it |
| `OL16314245W` | The Dark Forest | journal | 3 / 3 | premium only |
| `OL46390W` | The Foundation Trilogy | ebook | 3 / 3 | ebook |
| `OL46374W` | Nightfall | paperback | 1 / 2 | `S_HAS` holds one copy |

## A. Decision rules (`shelfsense advise MEMBER BOOK`, read-only)
The rules are checked in this order: suspended, at max loans, already holds the book, already on the waitlist, journal for a standard member,
no copy (waitlist), otherwise allow. Stores: PostgreSQL (members, copies, loans) and MongoDB (type-specific book data).

| # | Member | Book | Expected | Reason shown | What it shows |
|---|---|---|---|---|---|
| 1 | `S_STD` | `OL17610507W` | **ALLOW** | copy available | basic happy path |
| 2 | `S_PREM` | `OL46401W` | **ALLOW** | copy available | premium member |
| 3 | `S_STD` | `OL1963251W` | **ALLOW** | copy available | boundary: exactly one copy left |
| 4 | `S_STD` | `OL46398W` | **WAITLIST** | no copy available | no copy; alternatives with a free copy are listed |
| 5 | `S_PREM` | `OL46398W` | **WAITLIST** | no copy available | premium does not skip the queue |
| 6 | `S_SUSP` | `OL17610507W` | **REJECT** | member suspended | member rule |
| 7 | `S_FULL` | `OL17610507W` | **REJECT** | member at max loans | loan limit |
| 8 | `S_HAS` | `OL46374W` | **REJECT** | member already holds this book | duplicate loan |
| 9 | `S_WAIT` | `OL7967812W` | **REJECT** | member already on waitlist | duplicate waitlist entry |
| 10 | `S_STD` | `OL16314245W` | **REJECT** | journals are premium only | book type rule (MongoDB `book_type`) |
| 11 | `S_PREM` | `OL16314245W` | **ALLOW** | copy available | same journal, premium member |
| 12 | `S_STD` | `OL46390W` | **ALLOW** | copy available | ebook is fine for a standard member |
| 13 | `S_SUSP` | `OL46398W` | **REJECT** | member suspended | suspension wins over waitlist |
| 14 | `S_FULL` | `OL46398W` | **REJECT** | member at max loans | loan limit wins over waitlist |
| 15 | `S_PREM` | `OL7967812W` | **WAITLIST** | no copy available | not yet on that waitlist, so a new entry would be allowed |

Every REJECT and WAITLIST answer also lists alternatives with at least one free copy; their `via` column shows where each came from
(`same_author` from Neo4j, "also borrowed" from the Neo4j loan graph, similar text from Qdrant).
Example: `shelfsense advise S_STD OL46398W` lists *Isaac Asimov's Robots* (5 free, same author) as the first alternative.

## B. Redis cache
| # | Command | Expected |
|---|---|---|
| 16 | `shelfsense advise S_STD OL46398W` (first time) | WAITLIST, no "(cached)" marker |
| 17 | the same command again, within 5 minutes | same WAITLIST, now marked **(cached)** (answer came from Redis, not PostgreSQL) |
| 18 | `shelfsense eval stale-cache` | step 3 shows the stale cached answer (`waitlist`, `True`, TTL 300 s), step 4 after invalidation shows the correct `allow`; the demo also explains that `borrow()` re-checks in PostgreSQL under a row lock, so a stale answer can never oversell |

## C. Real borrowing and the waitlist (`shelfsense borrow`, writes to PostgreSQL)
Run in this order on a clean state (`shelfsense db load`):

| # | Command | Expected |
|---|---|---|
| 19 | `shelfsense borrow S_STD OL1963251W` | **ALLOW** (decision #1); the last copy is now taken, the book has 0 free copies |
| 20 | `shelfsense advise S_PREM OL1963251W` | **WAITLIST**: the copy taken in step 19 is gone (the cache was invalidated by the borrow) |
| 21 | `shelfsense borrow S_PREM OL1963251W` | **WAITLIST** (decision #2); a waitlist row is written |
| 22 | `shelfsense advise S_PREM OL1963251W` | **REJECT**: member already on waitlist |
| 23 | `shelfsense borrow S_STD OL1963251W` | **REJECT**: member already holds this book (decision #3; rejected decisions are logged too) |
| 24 | `shelfsense eval concurrency` | `S_STD` and `S_PREM` borrow the last copy at the same moment: in 20/20 rounds exactly one gets **allow** and the other **waitlist**, and available copies never go below 0 |

## D. Search and recommendations (Qdrant + PostgreSQL)
| # | Command | Expected |
|---|---|---|
| 25 | `shelfsense search "Foundation"` | table of books whose title contains the text, with genre and free copies (e.g. *Foundation* `OL46125W` has 0 free, *Foundation and Chaos* `OL16486W` has 2) |
| 26 | `shelfsense recommend "epic fantasy with dragons" --genre fantasy` | 5 fantasy books about dragons, all with at least one free copy, e.g. *Dragon path*, *Calling on dragons*, *Dealing with Dragons* (semantic search in Qdrant, filtered by genre, then joined with PostgreSQL availability) |
| 27 | `shelfsense similar OL46401W -n 5` | 5 books related to *The Caves of Steel*, mostly by Isaac Asimov (`via` = `same_author`), all with free copies |
| 28 | `shelfsense eval join` | Qdrant top 10 for the fantasy query, then PostgreSQL adds `free=x/y loans=n waiting=n` for each book |
| 29 | `shelfsense eval vectors` | recall@5 0.73, MRR 0.82 on the 20 labelled queries; ANN recall@10 about 0.99 at the lowest `ef`, 1.0 at the highest |

## E. Several databases at once
| # | Command | Expected |
|---|---|---|
| 30 | `shelfsense db status` | PostgreSQL 2906 members / 20000 books / 24512 loans / 401 waitlist; MongoDB 20000 books; Neo4j 20000 books and 24506 `BORROWED` edges (one edge per distinct member and book); Qdrant 20000 points |
| 31 | `shelfsense eval update-across` | through the Advisor: a title/description edit drops the Redis entry, re-embeds only that book in Qdrant (new vector, title) and renames the Neo4j node; `borrow` adds the `BORROWED` edge, `return` marks it returned; an added book appears in Neo4j and Qdrant, a deleted one disappears from both. No sync is run. The demo restores all data at the end |
| 32 | `shelfsense book add DEMO1 "Bees" "A. Author" science "Honey bees and hives."` then `shelfsense recommend "honey bees"`, then `shelfsense book edit DEMO1 --title "Wasps"`, `shelfsense book delete DEMO1` | the new book is recommendable immediately (Qdrant) without sync; after delete it is gone from every store |

## F. Interface
| # | Command | Expected |
|---|---|---|
| 33 | `shelfsense` | the full-screen TUI opens. Click `S_STD` in the member list and "No copies" book in the book list (Advise tab, F2): WAITLIST banner, rule trace (last rule failed) and alternatives. Recommend tab (F3): "epic fantasy with dragons", genre `fantasy`: same books as case 26 |
| 34 | TUI: select `S_STD` + the "last copy" book, Borrow (ALLOW); select `S_PREM`, Borrow (WAITLIST); select `S_STD` again, Librarian tab (F6), Return | "copy given to S_PREM from the waitlist"; Member tab shows S_PREM's loan. Run `shelfsense db load` afterwards to reset |
| 35 | TUI Scenarios tab (F8), Run all | summary "15/15 correct" |
| 36 | TUI Stores tab (F7) | row counts and latency of all five stores, Redis cache entries; Flush clears them |
| 37 | `shelfsense --json advise S_STD OL46398W` | the same answer as raw JSON (`outcome`, `reason`, `from_cache`, `alternatives`) |
| 38 | `shelfsense advise NOPE OL46398W` | **NEEDS_INFO**: member not found. Missing evidence is not guessed; the decision is stored with the reason (`--json` shows it) |

## G. Project II features
| # | Command | Expected |
|---|---|---|
| 39 | `shelfsense eval rules` | `S_FULL` is rejected under rules v1 (limit 3, decision #1); after v2 (limit 4) is published the same request is allowed (decision #2); decision #1 is explained with v1 and replays to *reject*. Data and rule versions are reset afterwards |
| 40 | `shelfsense explain 1` (after a borrow) / `shelfsense explain S_STD OL46398W` | the stored decision with its inputs, rule version and the rule-by-rule replay (`replay_ok`), or the same rule trace for a live check. TUI: Librarian tab, Enter on a decision or "Explain selected decision" |
| 41 | `shelfsense rules list`, `rules show 1`, `rules publish ...` | all rule versions with validity dates; publishing a version deletes cached decisions |
| 42 | `shelfsense recommend "coming-of-age adventure for young readers" --mode vector\|keyword\|hybrid` | three result lists; hybrid (reciprocal rank fusion, K=60) puts a relevant book first |
| 43 | `shelfsense eval hybrid` | vector recall@5 0.73 / MRR 0.82, keyword 0.51 / 0.68, hybrid 0.76 / 0.93 on the 20 frozen queries, plus one explained query |
| 44 | `shelfsense eval compare` | pgvector against Qdrant on the same 20 queries: recall, ANN recall at `ef` 16/64/128, filters at 5% and 50%, latency, index size, top-10 overlap 1.0 |
| 45 | `shelfsense eval stale` | one temporary book: before (all stores at version 1), stale (source v2, stores v1, search misses), after `refresh` (all v2, search hits); the book is deleted at the end |
| 46 | `shelfsense db derived` / `db refresh` | per store: current / stale / missing / orphans and the last refresh; refresh re-indexes only stale books |
| 47 | `shelfsense db check` | 21 data checks (counts, duplicates, cross-store agreement, registry, labels), exit code 1 on a failure |
| 48 | `shelfsense eval labels` | the label files match the SHA-256 hashes frozen in `data/labels/labels.lock.json` (20 queries, 15 loan cases) |

## H. Automatic checks
| Command | Expected |
|---|---|
| `shelfsense test` | 108 passed (on separate test databases; Neo4j and Qdrant are the real ones, with throw-away ids) |
| `shelfsense eval all` | all measurements and demos; 15/15 loan cases, 20/20 concurrency rounds, 34 safety tests, 21/21 data checks; writes `report/results.txt` and `results.json` |

## Suggested order for a live demo (about 5 minutes)
1. `shelfsense db status` (case 30): five databases, one dataset.
2. Cases 1, 4, 6, 10 and 8: the main outcomes and some rules.
3. Case 17: the cache.
4. Cases 19-23: a real borrow, the waitlist and rejection.
5. Cases 24 and 18: concurrency and the stale cache.
6. Case 26 or the TUI (33): the recommendations.
7. Cases 31 and 32: one change across all stores, a book added live.
8. `shelfsense db load` to reset.
