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

-- Per (legislator, ticker) estimated current stock position - a view, not a materialized
-- table, to keep a single source of truth (no cached value that could drift from the
-- trades/ticker_prices it's derived from). Cheap when queried scoped to one legislator_id -
-- confirmed via EXPLAIN QUERY PLAN that the filter pushes down to indexed lookups on
-- filings/trades/ticker_prices rather than aggregating the whole trade history first; an
-- unscoped query against this view does scan the full stock-trade history, so callers
-- should prefer `WHERE legislator_id = ?`. Dropped and recreated on every connect() (safe -
-- a view holds no data of its own) so its definition always matches this file exactly,
-- with no separate migration bookkeeping needed.
DROP VIEW IF EXISTS politician_ticker_positions;
CREATE VIEW politician_ticker_positions AS
WITH scoped_trades AS (
    SELECT
        f.legislator_id,
        t.ticker,
        t.transaction_type,
        t.transaction_date,
        t.price_at_transaction,
        (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid
    FROM trades t
    JOIN filings f ON f.id = t.filing_id
    WHERE t.asset_type IN ('ST', 'Stock')
      AND t.ticker IS NOT NULL AND t.ticker != ''
      AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')
),
aggregated AS (
    SELECT
        legislator_id, ticker,
        SUM(CASE WHEN transaction_type = 'purchase' THEN amount_mid ELSE -amount_mid END) AS net_position_estimate,
        SUM(CASE WHEN transaction_type = 'purchase' THEN 1 ELSE 0 END) AS buy_count,
        SUM(CASE WHEN transaction_type != 'purchase' THEN 1 ELSE 0 END) AS sell_count,
        MAX(transaction_date) AS latest_trade_date,
        SUM(CASE WHEN transaction_type = 'purchase' AND price_at_transaction IS NOT NULL
                  THEN price_at_transaction * amount_mid ELSE 0 END)
          / NULLIF(SUM(CASE WHEN transaction_type = 'purchase' AND price_at_transaction IS NOT NULL
                        THEN amount_mid ELSE 0 END), 0) AS avg_cost
    FROM scoped_trades
    GROUP BY legislator_id, ticker
)
SELECT
    l.id AS legislator_id, l.first_name, l.last_name, l.chamber,
    a.ticker, a.net_position_estimate, a.buy_count, a.sell_count,
    a.latest_trade_date, a.avg_cost, tp.current_price,
    CASE WHEN a.avg_cost IS NOT NULL AND tp.current_price IS NOT NULL
         THEN a.net_position_estimate * (tp.current_price - a.avg_cost) / a.avg_cost
    END AS estimated_gain
FROM aggregated a
JOIN legislators l ON l.id = a.legislator_id
JOIN ticker_prices tp ON tp.ticker = a.ticker
WHERE a.net_position_estimate > 0
  AND tp.price_status = 'active';

-- Per-legislator totals: total trades across every asset type (not just stock - "how active
-- a trader is this person overall" is a different question from the stock-specific view
-- above), plus stock-only totals (net worth, buy/sell counts, distinct tickers currently
-- held). stock_net_worth is NULL (not 0) when every held position lacks a cost basis, and
-- also NULL when the legislator holds no stock at all - distinct_tickers_held (0 vs >0)
-- disambiguates the two.
--
-- Deliberately does NOT query politician_ticker_positions (confirmed via EXPLAIN QUERY
-- PLAN, not assumed): referencing a view that itself has a GROUP BY from inside another
-- view/subquery defeats predicate pushdown entirely - SQLite fully materializes that inner
-- view for every legislator before applying an outer legislator_id filter, the exact
-- expensive-scan problem this design is meant to avoid. Inlining the same position logic
-- directly as correlated subqueries (verified: each one uses indexed SEARCHes scoped to
-- just one legislator, never a full scan) fixes it, at the cost of duplicating that SQL
-- rather than reusing the other view's definition - a deliberate trade of DRY-ness for a
-- real, measured cost difference, not a stylistic preference.
--
-- Same drop-and-recreate-on-every-connect() approach as the view above (a view holds no
-- data, so this is free and keeps the definition always in sync with this file).
DROP VIEW IF EXISTS politician_totals;
CREATE VIEW politician_totals AS
SELECT
    l.id AS legislator_id, l.first_name, l.last_name, l.chamber,

    (SELECT COUNT(*) FROM trades t JOIN filings f ON f.id = t.filing_id
     WHERE f.legislator_id = l.id) AS total_trades_all_types,

    (SELECT MAX(t.transaction_date) FROM trades t JOIN filings f ON f.id = t.filing_id
     WHERE f.legislator_id = l.id) AS latest_trade_date,

    -- Each of the next three subqueries requires an active ticker_prices row (matching
    -- politician_ticker_positions' exact scope) - caught via a real discrepancy (54 vs 64)
    -- while verifying against production: without this, distinct_tickers_held silently
    -- counted delisted/unpriced tickers that the position view excludes, giving two "current
    -- holdings" numbers that disagreed with each other.
    (SELECT COUNT(*) FROM (
        SELECT t.ticker,
            SUM(CASE WHEN t.transaction_type = 'purchase'
                     THEN (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0
                     ELSE -(t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 END) AS net_pos
        FROM trades t JOIN filings f ON f.id = t.filing_id
        WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock')
          AND t.ticker IS NOT NULL AND t.ticker != ''
          AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')
          AND EXISTS (SELECT 1 FROM ticker_prices tp WHERE tp.ticker = t.ticker AND tp.price_status = 'active')
        GROUP BY t.ticker HAVING net_pos > 0
    )) AS distinct_tickers_held,

    (SELECT COALESCE(SUM(CASE WHEN t.transaction_type = 'purchase' THEN 1 ELSE 0 END), 0)
     FROM trades t JOIN filings f ON f.id = t.filing_id
     WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock')
       AND t.ticker IS NOT NULL AND t.ticker != ''
       AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')
       AND EXISTS (SELECT 1 FROM ticker_prices tp WHERE tp.ticker = t.ticker AND tp.price_status = 'active')) AS total_stock_buy_count,

    (SELECT COALESCE(SUM(CASE WHEN t.transaction_type != 'purchase' THEN 1 ELSE 0 END), 0)
     FROM trades t JOIN filings f ON f.id = t.filing_id
     WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock')
       AND t.ticker IS NOT NULL AND t.ticker != ''
       AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')
       AND EXISTS (SELECT 1 FROM ticker_prices tp WHERE tp.ticker = t.ticker AND tp.price_status = 'active')) AS total_stock_sell_count,

    (SELECT SUM(pos.net_pos * tp.current_price / pos.avg_cost) FROM (
        SELECT s.ticker,
            SUM(CASE WHEN s.transaction_type = 'purchase' THEN s.amount_mid ELSE -s.amount_mid END) AS net_pos,
            SUM(CASE WHEN s.transaction_type = 'purchase' AND s.price_at_transaction IS NOT NULL
                     THEN s.price_at_transaction * s.amount_mid ELSE 0 END)
              / NULLIF(SUM(CASE WHEN s.transaction_type = 'purchase' AND s.price_at_transaction IS NOT NULL
                           THEN s.amount_mid ELSE 0 END), 0) AS avg_cost
        FROM (
            SELECT t.ticker, t.transaction_type, t.price_at_transaction,
                (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid
            FROM trades t JOIN filings f ON f.id = t.filing_id
            WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock')
              AND t.ticker IS NOT NULL AND t.ticker != ''
              AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')
        ) s
        GROUP BY s.ticker
    ) pos
    JOIN ticker_prices tp ON tp.ticker = pos.ticker
    WHERE pos.net_pos > 0 AND pos.avg_cost IS NOT NULL AND tp.price_status = 'active') AS stock_net_worth

FROM legislators l;

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
