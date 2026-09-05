-- Congressional Trading Disclosure Ingestor
-- SQLite schema — Phase 1 (ingestion)

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

-- One row per transaction line item within a filing. transaction_type and owner are
-- canonicalized here (both sources use different spellings/codes for the same values -
-- e.g. Senate spells out "Sale (Full)", House uses single-letter codes); that mapping
-- happens in the parser, not the DB. asset_type is left as free text rather than a CHECK
-- enum since the official asset-type code list is large (dozens of values) and not worth
-- hardcoding here.
CREATE TABLE IF NOT EXISTS trades (
    id                 INTEGER PRIMARY KEY,
    filing_id          INTEGER NOT NULL REFERENCES filings (id),
    ticker             TEXT,           -- nullable: some foreign ADRs are filed with no ticker
                                        -- in this field even though one appears in asset_name
    asset_name         TEXT NOT NULL,
    asset_type         TEXT,
    transaction_type   TEXT NOT NULL CHECK (transaction_type IN
                             ('purchase', 'sale_full', 'sale_partial', 'exchange')),
    transaction_date   TEXT NOT NULL,  -- ISO 8601 date
    notification_date  TEXT NOT NULL,  -- ISO 8601 date
    amount_low         INTEGER NOT NULL,
    amount_high        INTEGER,        -- nullable: the top disclosure bracket is open-ended
                                        -- (e.g. "$50,000,001+"); equals amount_low for a
                                        -- point-value amount (e.g. options expiring worthless)
    owner              TEXT NOT NULL CHECK (owner IN
                             ('self', 'spouse', 'joint', 'dependent_child')),
    comment            TEXT,
    raw_row_text       TEXT,           -- full raw text of this transaction line, catch-all
                                        -- for source-specific fields not otherwise modeled
                                        -- (e.g. House's cap-gains-over-$200 flag, per-row
                                        -- filing status) since Phase 3 scoring doesn't need
                                        -- them but they shouldn't be silently discarded
    UNIQUE (filing_id, asset_name, transaction_date, transaction_type, amount_low, owner)
);

CREATE INDEX IF NOT EXISTS idx_trades_filing_id ON trades (filing_id);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_transaction_date ON trades (transaction_date);
