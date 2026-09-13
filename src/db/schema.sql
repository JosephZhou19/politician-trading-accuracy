-- Congressional Trading Disclosure Ingestor
-- SQLite schema — Phase 1 (ingestion)

PRAGMA foreign_keys = ON;

-- Identity is inferred from (first_name, last_name, chamber) - neither source exposes a
-- stable per-person ID. COLLATE NOCASE keeps older ALL-CAPS Senate filings from creating
-- a duplicate row for the same person.
CREATE TABLE IF NOT EXISTS legislators (
    id            INTEGER PRIMARY KEY,
    first_name    TEXT NOT NULL COLLATE NOCASE,
    last_name     TEXT NOT NULL COLLATE NOCASE,
    chamber       TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    filer_status  TEXT NOT NULL CHECK (filer_status IN ('member', 'former_member', 'candidate')),
    UNIQUE (first_name, last_name, chamber)
);

-- chamber is duplicated from legislators (not derived via join) since the source's filing
-- ID is only unique within a chamber, and the UNIQUE constraint below needs it directly.
CREATE TABLE IF NOT EXISTS filings (
    id                  INTEGER PRIMARY KEY,
    legislator_id       INTEGER NOT NULL REFERENCES legislators (id),
    chamber             TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    external_filing_id  TEXT NOT NULL,
    filing_type         TEXT NOT NULL CHECK (filing_type IN ('ptr', 'annual', 'other')),
    is_amendment        INTEGER NOT NULL DEFAULT 0 CHECK (is_amendment IN (0, 1)),
    filing_date         TEXT,  -- nullable: needs_ocr filings have no reliable machine-read date
    source_url          TEXT NOT NULL,
    document_format     TEXT NOT NULL CHECK (document_format IN ('html', 'pdf', 'image')),
    raw_file_path       TEXT,
    raw_doc_hash        TEXT,
    fetched_at          TEXT NOT NULL,
    parsed_at           TEXT,
    parse_status        TEXT NOT NULL DEFAULT 'pending'
                             CHECK (parse_status IN ('pending', 'parsed', 'failed', 'needs_ocr')),
    -- Senate report titles say "for MM/DD/YYYY" - the original's own date for a normal
    -- filing, or the date of the ORIGINAL being corrected for an amendment (amendments carry
    -- no other reference to what they amend). Grouping by (legislator, nominal_date) clusters
    -- an original with its whole amendment chain. NULL on House - no such reference exists.
    nominal_date            TEXT,
    -- Precise "Filed MM/DD/YYYY @ H:MM AM/PM" timestamp (Senate only) for ordering same-day
    -- amendment chains, which plain filing_date can't distinguish. See reconcile_amendments.py.
    filed_at                TEXT,
    -- Explicit sequence number from "(Amendment N)" in the title, when present - more
    -- authoritative than inferring order from filed_at. NULL for older unnumbered amendments
    -- and non-amendment filings.
    amendment_number        INTEGER,
    superseded_by_filing_id INTEGER REFERENCES filings (id),
    -- Set when reconciliation found something it couldn't safely auto-resolve (e.g. an
    -- amendment whose nominal_date matches more than one original). NULL means clean.
    reconciliation_note     TEXT,
    UNIQUE (chamber, external_filing_id)
);

CREATE INDEX IF NOT EXISTS idx_filings_legislator_id ON filings (legislator_id);

-- transaction_type and owner are canonicalized here; the parser maps each source's own
-- spellings/codes onto these values. asset_type is free text since the official code list
-- is large. raw_row_text is a catch-all for source-specific fields not otherwise modeled.
--
-- source_row_number (the source's own "#" column, or parse order for House) is the dedup
-- key, not a composite of business fields - two legitimately distinct transactions (e.g.
-- two dependent children buying the same stock the same day for the same amount) can be
-- identical on every business field, so a composite key would silently drop one.
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
    -- Per-transaction "Filing Status" (New/Amended) from the House electronic system - NULL
    -- for Senate, which reconciles at the filing level instead (see reconcile_amendments.py).
    filing_status           TEXT,
    superseded_by_trade_id  INTEGER REFERENCES trades (id),
    reconciliation_note     TEXT,
    -- Fixed facts about this specific trade (all via yfinance's split/dividend-adjusted
    -- Open price, on the given date or the next trading day) - never change once set, so
    -- they live directly on the trade rather than in a lookup table. NULL for trades with
    -- no ticker, and for anything not yet backfilled or not yet reached (a horizon date
    -- that hasn't happened yet stays NULL until the daily catch-up job's date arrives).
    -- Current/ongoing price is a property of the ticker, not the trade - see ticker_prices.
    price_at_transaction    REAL,
    price_at_notification   REAL,
    price_30d               REAL,
    price_90d               REAL,
    price_180d              REAL,
    price_365d              REAL,
    UNIQUE (filing_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_trades_filing_id ON trades (filing_id);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_transaction_date ON trades (transaction_date);

-- One row per distinct ticker - current price is a shared, mutable fact about the stock,
-- not the trade, so it's stored once here and joined against every trade of that ticker
-- rather than repeated per-trade. Refreshed by the daily Finnhub trickle job.
-- current_price/price_updated_at are nullable: a ticker can accumulate zero_streak before
-- ever getting a real price (e.g. it delists the same day the trickle job first sees it).
CREATE TABLE IF NOT EXISTS ticker_prices (
    ticker            TEXT PRIMARY KEY,
    current_price     REAL,
    price_updated_at  TEXT,
    price_status      TEXT NOT NULL DEFAULT 'active' CHECK (price_status IN ('active', 'delisted')),
    zero_streak       INTEGER NOT NULL DEFAULT 0,
    last_checked_at   TEXT
);

-- Single-row bookmark for the trickle job: the last ticker it successfully checked, so a
-- run that hits its time budget before finishing the whole universe resumes right after
-- this ticker next time instead of restarting from the top (and risking never reaching the
-- tickers alphabetically near the end).
CREATE TABLE IF NOT EXISTS trickle_cursor (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    last_ticker TEXT
);

-- One row per scraper invocation, for observability once ingestion runs on a schedule.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id              INTEGER PRIMARY KEY,
    chamber         TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    filings_found   INTEGER,
    filings_new     INTEGER,
    filings_failed  INTEGER,
    status          TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed', 'failed')),
    error_message   TEXT
);
