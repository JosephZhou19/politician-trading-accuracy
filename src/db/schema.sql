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
    UNIQUE (filing_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_trades_filing_id ON trades (filing_id);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_transaction_date ON trades (transaction_date);
-- Partial - most rows are NULL here. reconcile_house_amendments queries this; see PLAN.md.
CREATE INDEX IF NOT EXISTS idx_trades_superseded_by_trade_id
    ON trades (superseded_by_trade_id) WHERE superseded_by_trade_id IS NOT NULL;

-- Delisting-detection state only, per ticker - NOT a price cache (current price is just the
-- latest row in ticker_daily_prices for that ticker; no reason to store it a second time).
-- Kept purely so update_ticker_daily_prices.py doesn't waste a yfinance call on a ticker
-- that's already confirmed dead, forever, and to know when a 'delisted' ticker is due for
-- its once-a-month safety-net recheck. Replaces the old, wider ticker_prices table (dropped
-- in _migrate) once current_price/sector/price_updated_at stopped having any writer.
CREATE TABLE IF NOT EXISTS ticker_status (
    ticker          TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'delisted')),
    zero_streak     INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT
);

-- Single-row bookmark for the one-time (possibly multi-run) full history backfill below:
-- the last ticker it successfully finished, so a run that hits its time budget before
-- covering the whole universe resumes right after this ticker next time instead of
-- restarting from the top (and risking never reaching the tickers alphabetically near the
-- end). The recurring incremental catch-up (update_ticker_daily_prices.py) has no cursor of
-- its own by design - see that script's docstring.
CREATE TABLE IF NOT EXISTS daily_price_backfill_cursor (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    last_ticker TEXT
);

-- Full daily price history, one row per ticker per actual trading day - the single source of
-- truth for every price this project needs. Built once via a heavy one-time backfill
-- (backfill_ticker_daily_prices.py), then kept current by a cheap incremental catch-up
-- (update_ticker_daily_prices.py) that only fetches each ticker's most recent few days per
-- run, batched across many tickers in one yfinance call to stay gentle on rate limits.
-- Replaced an earlier design of 6 fixed pre-computed point-prices per trade
-- (price_at_transaction/notification/30/90/180/365d, since removed from trades) - this
-- supports an arbitrary horizon/entry-date lookup instead of only those 6 fixed ones,
-- computed during local analysis (see src/analysis/price_lookup.py) rather than baked in
-- at write time. See conviction_analysis.py/sizing_timing_analysis.py for the kind of
-- analysis this feeds.
CREATE TABLE IF NOT EXISTS ticker_daily_prices (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    price  REAL NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- SPY's daily Open price (dividend-adjusted, matching how every individual stock's price
-- is fetched - a fair benchmark comparison needs the same adjustment convention on both
-- sides) - one row per actual trading day, written once by a one-time backfill, never
-- duplicated onto individual trades. Alpha vs. this benchmark is computed during local
-- analysis via a cheap indexed lookup (date is the primary key) using the same "roll forward
-- to the next trading day" logic as TickerHistory.price_on_or_after, not a plain equality
-- join - trade dates that fall on a weekend/holiday need the next available trading day's
-- price.
CREATE TABLE IF NOT EXISTS benchmark_prices (
    date  TEXT PRIMARY KEY,
    price REAL NOT NULL
);

-- Same idea as benchmark_prices, but one series per GICS sector (via its SPDR Select
-- Sector ETF - see SECTOR_ETFS in src/market/sectors.py) instead of one series for the
-- whole market. Lets alpha be computed against, say, Energy peers rather than the S&P 500
-- for a trader concentrated in one sector. sector is the exact string yfinance's own
-- info['sector'] returns (e.g. "Energy", "Financial Services") - not a GICS code, so
-- whatever eventually maps a ticker to a sector can join against this directly with no
-- translation. NOTE: nothing populates a per-ticker sector anywhere in this codebase yet
-- (ticker_prices.sector, which would have been that mapping, was dropped along with the
-- rest of that table - it never had a writer either) - this table itself has no consumer
-- until that gap is closed.
CREATE TABLE IF NOT EXISTS sector_benchmark_prices (
    sector TEXT NOT NULL,
    date   TEXT NOT NULL,
    price  REAL NOT NULL,
    PRIMARY KEY (sector, date)
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

