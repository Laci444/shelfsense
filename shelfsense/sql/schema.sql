-- PostgreSQL: system of record for business entities and decisions.
CREATE TABLE members (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tier        TEXT NOT NULL CHECK (tier IN ('standard', 'premium')),
    max_loans   INT  NOT NULL CHECK (max_loans > 0),
    suspended   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE books (
    id        TEXT PRIMARY KEY,      -- same id in Mongo, Neo4j, Qdrant, Redis keys
    title     TEXT NOT NULL,
    genre     TEXT NOT NULL,
    year      INT,
    book_type TEXT NOT NULL CHECK (book_type IN ('paperback', 'ebook', 'journal')),
    available INT  NOT NULL CHECK (available >= 0),
    total     INT  NOT NULL CHECK (total > 0),
    CHECK (available <= total)
);

CREATE TABLE loans (
    id          SERIAL PRIMARY KEY,
    member_id   TEXT NOT NULL REFERENCES members(id),
    book_id     TEXT NOT NULL REFERENCES books(id),
    loaned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_at      TIMESTAMPTZ NOT NULL,
    returned_at TIMESTAMPTZ
);
-- one active loan per member and book
CREATE UNIQUE INDEX uq_active_loan ON loans (member_id, book_id) WHERE returned_at IS NULL;

CREATE TABLE waitlist (
    id         SERIAL PRIMARY KEY,
    member_id  TEXT NOT NULL REFERENCES members(id),
    book_id    TEXT NOT NULL REFERENCES books(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (member_id, book_id)
);

CREATE TABLE decisions (
    id         SERIAL PRIMARY KEY,
    member_id  TEXT NOT NULL REFERENCES members(id),
    book_id    TEXT NOT NULL REFERENCES books(id),
    outcome    TEXT NOT NULL CHECK (outcome IN ('allow', 'waitlist', 'reject')),
    reason     TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
