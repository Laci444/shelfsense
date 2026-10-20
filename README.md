# ShelfSense

Library loan advisor: decides allow / waitlist / reject for a member and a book, and suggests similar books that are available.
Uses PostgreSQL, MongoDB, Redis, Neo4j and Qdrant (one reason each).

## Setup
Tested with Python 3.14.
```
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .                                      # installs the `shelfsense` command (editable: data/ and eval/ stay in the repo)
shelfsense db up                                      # starts the five databases in Docker
```
`requirements.txt` still has the pinned versions used for the measurements (`pip install -r requirements.txt` first for the same versions).

## Fill the databases (once)
```
shelfsense data build --books 20000     # or --books 500 for a small dataset; builds raw/, generated/, labels/
shelfsense db load                      # PostgreSQL + MongoDB
shelfsense db sync                      # Neo4j + Qdrant: slow (~10 min for 20k books on CPU), only to fill/rebuild
```
Run it from anywhere; set `SHELFSENSE_DATA=/some/dir` to put the data files somewhere else.

### How the data pipeline works
The databases are not filled directly from the internet. Building the files and loading them are separate steps:

| Step | Script | What it does | Output |
|---|---|---|---|
| 1 | `data/fetch_books.py --books N` | Streams the Open Library bulk dumps (4 GB works + 0.8 GB authors; nothing of the dump is stored) and keeps about N matching books, balanced over 10 genres. The authors scan takes a few minutes even for small N | `data/raw/books.json` |
| 2 | `data/generate.py` | Adds synthetic data with a fixed seed, all scaled to the number of books (members ~0.145x, loan history ~1x, active loans ~0.29x, waitlist ~0.02x): members, book type, copies, loans, waitlist. Also picks 8 scenario books and writes the loan labels | `data/generated/*.json`, `scenario.json`, `data/labels/loan_cases.json` |
| 3 | `data/build_labels.py` | Writes the retrieval labels: fixed queries and regex rules, applied to whatever books are in the dataset | `data/labels/interest_queries.json` |
| 4 | `shelfsense.load_sources` | Reads `data/generated/*.json` and loads PostgreSQL (`postgres.load`) and MongoDB (`mongo.load`, which adds the type-specific fields) | the two databases |
| 5 | `shelfsense.sync` | Builds the derived stores from PostgreSQL + MongoDB | Neo4j, Qdrant (Redis is flushed) |

`data.build_data` runs steps 1-3 (`--skip-fetch` reuses the existing `raw/books.json`; each step can also be run alone with `python -m data.<name>`).
`data/raw/books.json` and `data/generated/` are build outputs: they can be deleted and recreated. `load_sources` can be re-run at any
time to reset PostgreSQL and MongoDB. The "latest" Open Library dump changes over time, so a rebuild months later may return different books.

### Labels
Labels are the expected answers the tests and measurements check against; they are created by code, so they match any dataset size:
- `loan_cases.json` (15 cases) is written by `generate.py`. The scenario members (`S_STD`, `S_PREM`, `S_SUSP`, `S_FULL`, `S_HAS`, `S_WAIT`) and
  8 scenario books drawn from the dataset (copies and loans set up by hand, listed in `data/generated/scenario.json`) are fixed by design,
  and the expected outcome of each case follows from the loan rules, not from running the program.
- `interest_queries.json` (20 queries) is written by `build_labels.py`: a book is relevant if its genre is in the query's genre list and a
  regex matches its title, subjects or description. A query with no relevant book in a small dataset is skipped by the evaluation.

## Use it
`shelfsense` with no command opens the full-screen TUI. Left: live-search member and book lists. Tabs (F2–F9): **Advise** (verdict banner, rule-by-rule trace, alternatives, Borrow, export), **Recommend** (text + genre filter), **Book** (data from Postgres/Mongo/Neo4j/Redis + similar books), **Member** (loans, waitlist, history, return/leave waitlist), **Librarian** (return a book; the first eligible waitlisted member is served), **Stores** (stats, health, cache viewer/flush, reset data), **Scenarios** (runs the 15 loan test cases), **Demos** (eval demos with live log). Keys: F1 help, Ctrl+T theme, Ctrl+E export last answer to `exports/`, Ctrl+R refresh, Ctrl+Q quit. Borrow/return update Postgres, Redis and Neo4j at once, book add/edit/delete also Qdrant (`sync` is only a full rebuild/repair). Reset reloads Postgres/Mongo and rebuilds the Neo4j graph; the demos restore everything they touch. Commands (`shelfsense --help`, and `--help` on each group):
```
shelfsense advise S_STD OL46398W       # decision (+ alternatives if not allowed)
shelfsense borrow S_STD OL1963251W     # real loan in PostgreSQL (+ Neo4j edge)
shelfsense return S_STD OL1963251W     # return; serves the waitlist
shelfsense book add|edit|delete ...    # Neo4j node and Qdrant point follow at once
shelfsense recommend "epic fantasy with dragons" --genre fantasy
shelfsense similar OL46401W -n 5
shelfsense search "dune"
shelfsense --json advise S_STD OL46398W   # raw JSON
shelfsense db up | down | load | sync | status
shelfsense data build | fetch | generate | labels
shelfsense eval all | vectors | join | concurrency | stale-cache | update-across
shelfsense test
```
The example book ids are the scenario books of the current 20k dataset; a rebuilt dataset has other ids (see `data/generated/scenario.json`, or use `search`).

## Tests and measurements
User-facing demo scenarios (member, book, expected outcome for each feature) are in [TEST_CASES.md](TEST_CASES.md).
```
shelfsense test                  # 46 tests, run on separate test databases; no re-sync needed
shelfsense eval all              # all measurements and demos -> report/results.txt, results.json
```
Individual demos: `shelfsense eval vectors | join | concurrency | stale-cache | update-across`.
`shelfsense eval all` and the demos reset the PostgreSQL data to the generated state (cheap) and restore Neo4j and Qdrant themselves where they changed them; no `sync` is needed afterwards.

## Why each store
- PostgreSQL: loans and copy counts need transactions and constraints.
- MongoDB: book records differ by type (paperback, ebook, journal).
- Redis: expiring, fast-changing state (cached decisions, reading lists).
- Neo4j: relationships (same author, "also borrowed").
- Qdrant: meaning-based search over book blurbs with metadata filters.

PostgreSQL and MongoDB are the sources of record; Redis, Neo4j and Qdrant are derived and carry `source_id` / `refreshed_at`.
pgvector is not used (Qdrant is the only vector store).

## Documents
- `report/report.md` – the report; `report/presentation.md` – week-7 material (data model, vector search, measurement plan)
- `data/raw/SOURCES.md` – data sources and licences
