# Changelog

## Unreleased (Project II, since tag `v1`)

### Added
- **Versioned business rules.** Rules are now data in a new `rules` table
  (version, JSONB params, `valid_from`/`valid_to`, note) instead of constants in code.
  Exactly one version is active (enforced by a unique partial index). v1 is seeded
  with the original behaviour (21-day loans, limits 3/6, journals restricted for premium).
- `shelfsense rules list | show | publish` (`--loan-days`, `--standard-max`,
  `--premium-max`, `--note`). Publishing closes the active version, creates the next one
  and flushes cached decisions in Redis.
- **Pure rule evaluation** (`shelfsense/rules.py`): `evaluate` / `decide` return the
  outcome, the reason and the deciding rule id (`r0_evidence` ... `r6_available`).
- **`needs_info` outcome.** An unknown member or book, or a tier without a limit, now
  gives a recorded `needs_info` decision instead of an error.
- **Decision trail.** Each decision stores the rule version, the deciding rule id, the
  requested member/book, and the evidence used (ids and versions of the member and book,
  loan count, holds, waitlist) as JSONB. Waitlist promotions are recorded too.
- **`version` / `updated_at` on `members` and `books`**, bumped by a trigger on update.
- `shelfsense explain <decision-id>`: shows the stored decision, the rules version it
  used, the evidence, and a replay of that evidence under the same version (it stays
  reproducible after newer rules are published). `explain MEMBER BOOK` still shows the
  live rule table.
- `shelfsense db schema`: applies the (now idempotent) schema to an existing database.
- TUI: `needs_info` colour/icon and a rules-version column in the decisions table.
- Tests: `tests/test_rules.py` (10 cases) for schema, versions, `needs_info`, publishing,
  replay of old decisions, loan days, and promotions.

### Changed
- `schema.sql` is idempotent and upgrades a Project I database in place (backfills the
  new `decisions` columns; `member_id`/`book_id` are now nullable).
- Loan period and loan limits come from the active rules; `members.max_loans` is kept
  but no longer decides.
- PostgreSQL image is now `pgvector/pgvector:0.8.0-pg16` (same data volume; the
  extension is not used yet).
- Test fixtures apply the schema idempotently.

### Planned next
Hybrid search, pgvector vs Qdrant comparison, derived-state registry with
before/stale/after refresh demo, safety/duplicate tests, data validation, frozen labels.
