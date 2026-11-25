-- PostgreSQL: system of record for business entities, rules and decisions.
-- Idempotent: safe to run on an empty database (docker init) and again on an existing one
-- (`shelfsense db schema`), where it upgrades the tables in place and keeps the data.

-- Versioned rules: a decision stores the version it used, so it can be explained after the rules change.
CREATE TABLE IF NOT EXISTS rules (
    version    INT PRIMARY KEY,
    params     JSONB NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to   TIMESTAMPTZ,              -- NULL = the active version
    note       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_rules ON rules ((TRUE)) WHERE valid_to IS NULL;
INSERT INTO rules (version, params, note) VALUES
    (1, '{"loan_days": 21, "max_loans": {"standard": 3, "premium": 6}, "restricted_types": {"journal": ["premium"]}}',
     'initial rules')
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS members (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tier        TEXT NOT NULL CHECK (tier IN ('standard', 'premium')),
    max_loans   INT  NOT NULL CHECK (max_loans > 0),   -- contract default; the rules decide the effective limit
    suspended   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS books (
    id        TEXT PRIMARY KEY,      -- same id in Mongo, Neo4j, Qdrant, Redis keys
    title     TEXT NOT NULL,
    genre     TEXT NOT NULL,
    year      INT,
    book_type TEXT NOT NULL CHECK (book_type IN ('paperback', 'ebook', 'journal')),
    available INT  NOT NULL CHECK (available >= 0),
    total     INT  NOT NULL CHECK (total > 0),
    CHECK (available <= total)
);

-- every change of a member or book row raises its version: decisions record which version they read
ALTER TABLE members ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;
ALTER TABLE members ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE books ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;
ALTER TABLE books ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION bump_version() RETURNS trigger AS $$
BEGIN
    NEW.version := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS members_version ON members;
CREATE TRIGGER members_version BEFORE UPDATE ON members FOR EACH ROW EXECUTE FUNCTION bump_version();
DROP TRIGGER IF EXISTS books_version ON books;
CREATE TRIGGER books_version BEFORE UPDATE ON books FOR EACH ROW EXECUTE FUNCTION bump_version();

CREATE TABLE IF NOT EXISTS loans (
    id          SERIAL PRIMARY KEY,
    member_id   TEXT NOT NULL REFERENCES members(id),
    book_id     TEXT NOT NULL REFERENCES books(id),
    loaned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_at      TIMESTAMPTZ NOT NULL,
    returned_at TIMESTAMPTZ
);
-- one active loan per member and book
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_loan ON loans (member_id, book_id) WHERE returned_at IS NULL;

CREATE TABLE IF NOT EXISTS waitlist (
    id         SERIAL PRIMARY KEY,
    member_id  TEXT NOT NULL REFERENCES members(id),
    book_id    TEXT NOT NULL REFERENCES books(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (member_id, book_id)
);

-- Decision records. member_id/book_id stay NULL when the request named an unknown member or book
-- (outcome needs_info); requested_* always keep what was asked.
CREATE TABLE IF NOT EXISTS decisions (
    id         SERIAL PRIMARY KEY,
    member_id  TEXT REFERENCES members(id),
    book_id    TEXT REFERENCES books(id),
    outcome    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE decisions ALTER COLUMN member_id DROP NOT NULL;
ALTER TABLE decisions ALTER COLUMN book_id DROP NOT NULL;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS requested_member TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS requested_book TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS rule_version INT REFERENCES rules(version);
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS rule_id TEXT;      -- the rule that decided, e.g. r2_limit
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS inputs JSONB NOT NULL DEFAULT '{}';  -- values read, with source ids and versions
UPDATE decisions SET requested_member = member_id WHERE requested_member IS NULL;
UPDATE decisions SET requested_book = book_id WHERE requested_book IS NULL;
UPDATE decisions SET rule_version = 1 WHERE rule_version IS NULL;
ALTER TABLE decisions ALTER COLUMN requested_member SET NOT NULL;
ALTER TABLE decisions ALTER COLUMN requested_book SET NOT NULL;
ALTER TABLE decisions ALTER COLUMN rule_version SET NOT NULL;
ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_outcome_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_outcome_check
    CHECK (outcome IN ('allow', 'waitlist', 'reject', 'needs_info'));

-- pgvector (derived): book embeddings inside PostgreSQL, so they can be compared with Qdrant.
-- No foreign key to books on purpose: `db load` truncates books and must not wipe the derived vectors.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS book_vectors (
    book_id     TEXT PRIMARY KEY,
    embedding   vector(384) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
