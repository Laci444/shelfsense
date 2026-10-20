# Data sources

- **Books** (20,685), all from Open Library (https://openlibrary.org, public domain / CC0), snapshot in
  `data/raw/books.json`, fetched 2026-10-19 (the numbers in the report are for this snapshot):
  - 437 books via the Open Library Search/Works API (an earlier version of `data/fetch_books.py`). These include
    the scenario books of this snapshot (`data/generated/scenario.json`).
  - 20,248 books via the bulk dumps, streamed by an earlier `data/fetch_dump.py`. Both scripts are now merged into
    `data/fetch_books.py --books N`, which uses the dumps only (so new datasets have no API-fetched books and no page counts).
    It streams
    `ol_dump_works_latest` and `ol_dump_authors_latest` (nothing stored on disk). Kept only works with a description >= 200 chars
    (mostly ASCII), a title, subjects and an author. Authors are resolved from the authors dump.
  - Genre of dump books comes from subject keywords (10 genres, first matching genre wins).
    This is a heuristic: some labels are noisy, and horror/fantasy/cooking are thin.
  - Pages of dump books are random (the dump has no page count).
  - Per genre: science_fiction 1996+, fantasy 1583+, horror 740+, mystery 2500+, romance 2500+, poetry 2500+,
    cooking 929+, biography 2500+, science 2500+, history 2500+ (plus the original 437 spread over them).
- **Synthetic** (`data/generate.py`, seed 42): 3,006 members, `book_type` (paperback/ebook/journal),
  copy counts, type-specific MongoDB fields, 24,691 loans (history + active, popularity-skewed),
  401 waitlist entries (counts of this snapshot; `generate.py` now scales them with the number of books). Book type, copies and loans are NOT real catalogue data.
- **Size note:** the course PDF suggests 200-1,000 records per store; this project deliberately uses more so that
  Qdrant builds its HNSW index with default thresholds and the data is closer to real life.

## Test labels (written before tuning)
- `data/labels/loan_cases.json`: 15 loan decisions on fixed scenario members/books (`S_*`).
  Rules, checked in order: suspended -> reject; at max loans -> reject; already holds book -> reject;
  already on waitlist -> reject; journal + standard tier -> reject;
  no copy available -> waitlist; otherwise allow.
- `data/labels/interest_queries.json`: 20 free-text queries; relevance = genre in list AND
  regex match on title/subjects/description (rule stored per query), built by `data/build_labels.py`.
  The rules were fixed before tuning and re-applied unchanged to the larger corpus. Queries with many
  relevant books cap recall@5; this is noted in the report.

Rebuild: `shelfsense data build --books 20000` (writes `raw/books.json`, `generated/*`, `labels/*`; `--books 500` gives a small dataset),
then `shelfsense db load && shelfsense db sync` (sync embeds on CPU, ~10 min for 20k books).
