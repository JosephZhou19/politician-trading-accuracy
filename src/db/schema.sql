-- Congressional Trading Disclosure Ingestor
-- SQLite schema — Phase 1 (ingestion)

PRAGMA foreign_keys = ON;

-- Identity is inferred purely from (first_name, last_name, chamber) since neither
-- Senate eFD nor House Clerk exposes a stable per-person ID. COLLATE NOCASE on the name
-- columns makes both the UNIQUE constraint and lookups case-insensitive, since older
-- Senate filings render names in ALL CAPS while newer ones don't - without this, the
-- same senator ends up as two separate rows depending on which era's filing hit first.
CREATE TABLE IF NOT EXISTS legislators (
    id            INTEGER PRIMARY KEY,
    first_name    TEXT NOT NULL COLLATE NOCASE,
    last_name     TEXT NOT NULL COLLATE NOCASE,
    chamber       TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    filer_status  TEXT NOT NULL CHECK (filer_status IN ('member', 'former_member', 'candidate')),
    UNIQUE (first_name, last_name, chamber)
);

-- chamber is duplicated from legislators (not derived via join) because the source's own
-- filing ID is only unique within a chamber, and the UNIQUE constraint below needs it
-- directly. Ingest code keeps it consistent with legislator_id's chamber.
CREATE TABLE IF NOT EXISTS filings (
    id                  INTEGER PRIMARY KEY,
    legislator_id       INTEGER NOT NULL REFERENCES legislators (id),
    chamber             TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    external_filing_id  TEXT NOT NULL,
    filing_type         TEXT NOT NULL CHECK (filing_type IN ('ptr', 'annual', 'other')),
    is_amendment        INTEGER NOT NULL DEFAULT 0 CHECK (is_amendment IN (0, 1)),
    filing_date         TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    document_format     TEXT NOT NULL CHECK (document_format IN ('html', 'pdf', 'image')),
    raw_file_path       TEXT,
    raw_doc_hash        TEXT,
    fetched_at          TEXT NOT NULL,
    parsed_at           TEXT,
    parse_status        TEXT NOT NULL DEFAULT 'pending'
                             CHECK (parse_status IN ('pending', 'parsed', 'failed', 'needs_ocr')),
    UNIQUE (chamber, external_filing_id)
);

CREATE INDEX IF NOT EXISTS idx_filings_legislator_id ON filings (legislator_id);

-- transaction_type and owner are canonicalized here; the parser maps each source's own
-- spellings/codes onto these values. asset_type is free text rather than a CHECK enum
-- since the official code list is large. raw_row_text is a catch-all for source-specific
-- fields not otherwise modeled (e.g. House's cap-gains-over-$200 flag).
--
-- source_row_number (the source's own "#" column for Senate, or parse order for House,
-- which has no equivalent) is the dedup key, not a composite of business fields - a real
-- Whitehouse filing had two dependent children each buy the same stock, same day, same
-- amount bracket, both with an empty comment: completely legitimate distinct transactions
-- that are indistinguishable on ticker/date/type/amount/owner/comment alone. A composite
-- key silently dropped the second one as a "duplicate", losing real trade data.
CREATE TABLE IF NOT EXISTS trades (
    id                 INTEGER PRIMARY KEY,
    filing_id          INTEGER NOT NULL REFERENCES filings (id),
    source_row_number  INTEGER NOT NULL,
    ticker             TEXT,
    asset_name         TEXT NOT NULL,
    asset_type         TEXT,
    transaction_type   TEXT NOT NULL CHECK (transaction_type IN
                             ('purchase', 'sale_full', 'sale_partial', 'exchange')),
    transaction_date   TEXT NOT NULL,
    notification_date  TEXT NOT NULL,
    amount_low         INTEGER NOT NULL,
    amount_high        INTEGER,
    owner              TEXT NOT NULL CHECK (owner IN
                             ('self', 'spouse', 'joint', 'dependent_child')),
    comment            TEXT,
    raw_row_text       TEXT,
    UNIQUE (filing_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_trades_filing_id ON trades (filing_id);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_transaction_date ON trades (transaction_date);
