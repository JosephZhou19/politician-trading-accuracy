-- Congressional Trading Disclosure Ingestor
-- SQLite schema — Phase 1 (ingestion)
--
-- `trades` is intentionally not in this file yet — still being designed.

PRAGMA foreign_keys = ON;

-- One row per filer. Identity is inferred purely from (first_name, last_name, chamber)
-- since neither Senate eFD nor House Clerk exposes a stable per-person ID.
CREATE TABLE IF NOT EXISTS legislators (
    id            INTEGER PRIMARY KEY,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    chamber       TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    filer_status  TEXT NOT NULL CHECK (filer_status IN ('member', 'former_member', 'candidate')),
    UNIQUE (first_name, last_name, chamber)
);

-- One row per disclosure document. `chamber` is duplicated from legislators here
-- (not derived via join) because the source's own filing ID is only unique within
-- a chamber, so the UNIQUE constraint below needs it directly. Ingest code is
-- responsible for keeping it consistent with legislator_id's chamber.
CREATE TABLE IF NOT EXISTS filings (
    id                  INTEGER PRIMARY KEY,
    legislator_id       INTEGER NOT NULL REFERENCES legislators (id),
    chamber             TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    external_filing_id  TEXT NOT NULL,
    filing_type         TEXT NOT NULL CHECK (filing_type IN ('ptr', 'annual', 'other')),
    is_amendment        INTEGER NOT NULL DEFAULT 0 CHECK (is_amendment IN (0, 1)),
    filing_date         TEXT NOT NULL,  -- ISO 8601 date, e.g. '2023-01-25'
    source_url          TEXT NOT NULL,
    document_format     TEXT NOT NULL CHECK (document_format IN ('html', 'pdf')),
    raw_file_path       TEXT,
    raw_doc_hash        TEXT,
    fetched_at          TEXT NOT NULL,  -- ISO 8601 timestamp
    parsed_at           TEXT,
    parse_status        TEXT NOT NULL DEFAULT 'pending'
                             CHECK (parse_status IN ('pending', 'parsed', 'failed', 'needs_ocr')),
    UNIQUE (chamber, external_filing_id)
);

CREATE INDEX IF NOT EXISTS idx_filings_legislator_id ON filings (legislator_id);
