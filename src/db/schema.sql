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

-- SPY's daily Open price (dividend-adjusted, matching how every individual stock's price
-- is fetched - a fair benchmark comparison needs the same adjustment convention on both
-- sides) - one row per actual trading day, written once by a one-time backfill, never
-- duplicated onto individual trades. Alpha vs. this benchmark is computed in the views via
-- a cheap indexed lookup (date is the primary key) using the same "roll forward to the next
-- trading day" logic as TickerHistory.price_on_or_after, not a plain equality join - trade
-- dates that fall on a weekend/holiday need the next available trading day's price.
CREATE TABLE IF NOT EXISTS benchmark_prices (
    date  TEXT PRIMARY KEY,
    price REAL NOT NULL
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
-- above), plus stock-only totals. Two distinct scopes among the stock columns, named to
-- keep them apart after a real mix-up already happened once with this view (see below):
--   - "held" columns (distinct_tickers_held, total_stock_buy_count, total_stock_sell_count,
--     stock_net_worth, stock_total_cost_basis, win_count) - scoped to CURRENTLY HELD,
--     actively-priced positions only, exactly matching politician_ticker_positions.
--   - "all-time" columns (first/last_stock_trade_date, total_stock_dollar_volume) -
--     every stock trade ever, held or since exited, delisted or not - a broader question
--     about lifetime trading activity, not current holdings.
-- stock_net_worth/gain_pct/win_rate_pct are NULL (not 0) when every held position lacks a
-- cost basis, and also NULL when the legislator holds no stock at all - distinct_tickers_held
-- (0 vs >0) disambiguates the two.
--
-- Deliberately does NOT query politician_ticker_positions (confirmed via EXPLAIN QUERY
-- PLAN, not assumed): referencing a view that itself has a GROUP BY from inside another
-- view/subquery defeats predicate pushdown entirely - SQLite fully materializes that inner
-- view for every legislator before applying an outer legislator_id filter, the exact
-- expensive-scan problem this design is meant to avoid. Inlining the same position logic
-- directly as correlated subqueries (verified: each one uses indexed SEARCHes scoped to
-- just one legislator, never a full scan) fixes it, at the cost of duplicating that SQL
-- rather than reusing the other view's definition - a deliberate trade of DRY-ness for a
-- real, measured cost difference, not a stylistic preference. That duplication already
-- caused one real bug (distinct_tickers_held silently including delisted tickers the
-- position view excludes, 64 vs 54 for the same legislator) - fixed, with a regression
-- test, by requiring an active ticker_prices row on every "held" column consistently.
--
-- The derived ratio columns (years_active, gain_pct, win_rate_pct) are computed in an outer
-- SELECT layer over the raw correlated-subquery values, since a column can't reference a
-- sibling column's alias within the same SELECT list.
--
-- Same drop-and-recreate-on-every-connect() approach as the view above (a view holds no
-- data, so this is free and keeps the definition always in sync with this file).
DROP VIEW IF EXISTS politician_totals;
CREATE VIEW politician_totals AS
SELECT
    legislator_id, first_name, last_name, chamber,
    total_trades_all_types, latest_trade_date,
    first_stock_trade_date, last_stock_trade_date,
    CASE WHEN first_stock_trade_date IS NOT NULL
         THEN (julianday(last_stock_trade_date) - julianday(first_stock_trade_date)) / 365.25
    END AS years_active,
    total_stock_dollar_volume,
    trades_with_alpha_data, avg_1yr_alpha_pct,
    distinct_tickers_held, total_stock_buy_count, total_stock_sell_count,
    stock_net_worth, stock_total_cost_basis,
    CASE WHEN stock_net_worth IS NOT NULL THEN stock_net_worth - stock_total_cost_basis END AS total_estimated_gain,
    CASE WHEN stock_total_cost_basis IS NOT NULL AND stock_total_cost_basis != 0
         THEN (stock_net_worth - stock_total_cost_basis) / stock_total_cost_basis * 100
    END AS gain_pct,
    win_count,
    CASE WHEN distinct_tickers_held > 0
         THEN CAST(win_count AS REAL) / distinct_tickers_held * 100
    END AS win_rate_pct
FROM (
    SELECT
        l.id AS legislator_id, l.first_name, l.last_name, l.chamber,

        (SELECT COUNT(*) FROM trades t JOIN filings f ON f.id = t.filing_id
         WHERE f.legislator_id = l.id) AS total_trades_all_types,

        (SELECT MAX(t.transaction_date) FROM trades t JOIN filings f ON f.id = t.filing_id
         WHERE f.legislator_id = l.id) AS latest_trade_date,

        (SELECT MIN(t.transaction_date) FROM trades t JOIN filings f ON f.id = t.filing_id
         WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock')
           AND t.ticker IS NOT NULL AND t.ticker != ''
           AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')) AS first_stock_trade_date,

        (SELECT MAX(t.transaction_date) FROM trades t JOIN filings f ON f.id = t.filing_id
         WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock')
           AND t.ticker IS NOT NULL AND t.ticker != ''
           AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')) AS last_stock_trade_date,

        (SELECT COALESCE(SUM((t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0), 0)
         FROM trades t JOIN filings f ON f.id = t.filing_id
         WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock')
           AND t.ticker IS NOT NULL AND t.ticker != ''
           AND t.transaction_type IN ('purchase', 'sale_full', 'sale_partial')) AS total_stock_dollar_volume,

        -- All-time alpha vs. SPY (same fixed-window, dollar-weighted methodology as
        -- politician_yearly_activity's avg_1yr_alpha_pct - see that view's header comment
        -- for why this normalizes for tenure, not just market conditions). Deliberately
        -- inlined per-trade rather than referencing politician_yearly_activity: same
        -- predicate-pushdown reasoning as the rest of this view.
        (SELECT COUNT(*) FROM trades t JOIN filings f ON f.id = t.filing_id
         WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock') AND t.transaction_type = 'purchase'
           AND t.price_at_transaction IS NOT NULL AND t.price_365d IS NOT NULL
           AND (SELECT price FROM benchmark_prices WHERE date >= t.transaction_date
                  AND date <= date(t.transaction_date, '+10 days') ORDER BY date ASC LIMIT 1) IS NOT NULL
           AND (SELECT price FROM benchmark_prices WHERE date >= date(t.transaction_date, '+365 days')
                  AND date <= date(t.transaction_date, '+375 days') ORDER BY date ASC LIMIT 1) IS NOT NULL
        ) AS trades_with_alpha_data,

        -- SQLite has no LATERAL join (confirmed directly: "near SELECT: syntax error") -
        -- the per-trade SPY lookups are computed once in this derived table's SELECT list
        -- instead, same as politician_yearly_activity's per_trade CTE.
        (SELECT
            SUM(CASE WHEN spy_txn IS NOT NULL AND spy_365d IS NOT NULL
                     THEN ((price_365d - price_at_transaction) / price_at_transaction
                           - (spy_365d - spy_txn) / spy_txn) * amount_mid
                ELSE 0 END)
            / NULLIF(SUM(CASE WHEN spy_txn IS NOT NULL AND spy_365d IS NOT NULL THEN amount_mid ELSE 0 END), 0) * 100
         FROM (
             SELECT t.price_at_transaction, t.price_365d,
                 (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid,
                 (SELECT price FROM benchmark_prices WHERE date >= t.transaction_date
                    AND date <= date(t.transaction_date, '+10 days') ORDER BY date ASC LIMIT 1) AS spy_txn,
                 (SELECT price FROM benchmark_prices WHERE date >= date(t.transaction_date, '+365 days')
                    AND date <= date(t.transaction_date, '+375 days') ORDER BY date ASC LIMIT 1) AS spy_365d
             FROM trades t JOIN filings f ON f.id = t.filing_id
             WHERE f.legislator_id = l.id AND t.asset_type IN ('ST', 'Stock') AND t.transaction_type = 'purchase'
               AND t.price_at_transaction IS NOT NULL AND t.price_365d IS NOT NULL
         )
        ) AS avg_1yr_alpha_pct,

        -- Each of the next six subqueries requires an active ticker_prices row (matching
        -- politician_ticker_positions' exact "held" scope) - see the header comment above.
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
        WHERE pos.net_pos > 0 AND pos.avg_cost IS NOT NULL AND tp.price_status = 'active') AS stock_net_worth,

        (SELECT SUM(pos.net_pos) FROM (
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
        WHERE pos.net_pos > 0 AND pos.avg_cost IS NOT NULL AND tp.price_status = 'active') AS stock_total_cost_basis,

        (SELECT COUNT(*) FROM (
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
        WHERE pos.net_pos > 0 AND pos.avg_cost IS NOT NULL AND tp.price_status = 'active'
          AND tp.current_price > pos.avg_cost) AS win_count

    FROM legislators l
);

-- Per (legislator, year) historical activity - a real calendar year of trades, not a
-- rolling window. All-time/historical scope throughout (no ticker_prices join, no "still
-- actively priced today" requirement) - a legislator's real 2015 trade counts toward 2015
-- even if that ticker has since delisted; restricting to still-live tickers would erase
-- real history for no good reason. No cross-view reference either, so none of the
-- predicate-pushdown caution that shaped politician_totals applies here - this is a
-- single, direct GROUP BY over trades/filings/legislators, the same shape already proven
-- (in politician_ticker_positions) to push a legislator_id filter down to indexed lookups.
--
-- "Yearly gain" is deliberately NOT current unrealized gain split by year - a 2015 trade
-- evaluated at today's price has had 11 years to grow, an trade from last year has had
-- one, so comparing them that way would mostly measure elapsed time, not trading skill.
-- True realized gain needs lot-matching (buys to the sells that closed them), which is its
-- own separate, harder project, deliberately not attempted here. Instead this uses each
-- trade's already-backfilled price_365d column - the real price exactly 365 days after
-- that specific transaction - giving a fair, fixed-window return comparable across years.
-- Recent years will show partial or NULL 1yr figures until a full 365 days has actually
-- elapsed since those trades - expected, not a bug. trades_with_1yr_data is the trust
-- indicator: a low count means don't read much into that year's return/win-rate figures.
--
-- Alpha vs. S&P 500 (avg_1yr_alpha_pct / alpha_win_rate_1yr_pct) answers the "someone who's
-- been doing this for years isn't automatically ranked higher than someone who started
-- recently" problem: each trade's own 1yr return is compared only against what SPY did
-- over that SAME fixed window, not against another trade's window or today's price - a
-- 2015 trade and a 2024 trade are equally comparable, each only needing to beat the market
-- during its own year. The two SPY lookups (per trade, not per aggregate expression - see
-- the per_trade CTE) use the same "roll forward to the next trading day" logic as
-- TickerHistory.price_on_or_after, expressed as an indexed range scan (date is the primary
-- key on benchmark_prices) rather than requiring an exact date match.
DROP VIEW IF EXISTS politician_yearly_activity;
CREATE VIEW politician_yearly_activity AS
WITH per_trade AS (
    SELECT
        f.legislator_id,
        CAST(strftime('%Y', t.transaction_date) AS INTEGER) AS year,
        t.asset_type, t.transaction_type, t.ticker, t.price_at_transaction, t.price_365d,
        (t.amount_low + COALESCE(t.amount_high, t.amount_low)) / 2.0 AS amount_mid,
        CASE WHEN t.asset_type IN ('ST', 'Stock') AND t.transaction_type = 'purchase' THEN
            (SELECT price FROM benchmark_prices
             WHERE date >= t.transaction_date AND date <= date(t.transaction_date, '+10 days')
             ORDER BY date ASC LIMIT 1)
        END AS spy_price_at_transaction,
        CASE WHEN t.asset_type IN ('ST', 'Stock') AND t.transaction_type = 'purchase' THEN
            (SELECT price FROM benchmark_prices
             WHERE date >= date(t.transaction_date, '+365 days') AND date <= date(t.transaction_date, '+375 days')
             ORDER BY date ASC LIMIT 1)
        END AS spy_price_365d
    FROM trades t
    JOIN filings f ON f.id = t.filing_id
)
SELECT
    p.legislator_id,
    l.first_name, l.last_name, l.chamber,
    p.year,

    COUNT(*) AS total_trades_all_types,

    SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type = 'purchase'
             THEN 1 ELSE 0 END) AS stock_buy_count,
    SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type IN ('sale_full', 'sale_partial')
             THEN 1 ELSE 0 END) AS stock_sell_count,

    COUNT(DISTINCT CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.ticker IS NOT NULL AND p.ticker != ''
                        THEN p.ticker END) AS distinct_tickers_traded,

    SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type IN ('purchase', 'sale_full', 'sale_partial')
             THEN p.amount_mid ELSE 0 END) AS total_stock_dollar_volume,

    SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type = 'purchase'
             AND p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
             THEN 1 ELSE 0 END) AS trades_with_1yr_data,

    SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type = 'purchase'
             AND p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
             THEN (p.price_365d - p.price_at_transaction) / p.price_at_transaction * p.amount_mid
             ELSE 0 END)
      / NULLIF(SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type = 'purchase'
                        AND p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
                   THEN p.amount_mid ELSE 0 END), 0) * 100 AS avg_1yr_return_pct,

    SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type = 'purchase'
             AND p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
             AND p.price_365d > p.price_at_transaction
             THEN 1 ELSE 0 END) * 100.0
      / NULLIF(SUM(CASE WHEN p.asset_type IN ('ST', 'Stock') AND p.transaction_type = 'purchase'
                        AND p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
                   THEN 1 ELSE 0 END), 0) AS win_rate_1yr_pct,

    SUM(CASE WHEN p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
             AND p.spy_price_at_transaction IS NOT NULL AND p.spy_price_365d IS NOT NULL
             THEN 1 ELSE 0 END) AS trades_with_alpha_data,

    SUM(CASE WHEN p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
             AND p.spy_price_at_transaction IS NOT NULL AND p.spy_price_365d IS NOT NULL
             THEN ((p.price_365d - p.price_at_transaction) / p.price_at_transaction
                   - (p.spy_price_365d - p.spy_price_at_transaction) / p.spy_price_at_transaction) * p.amount_mid
             ELSE 0 END)
      / NULLIF(SUM(CASE WHEN p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
                        AND p.spy_price_at_transaction IS NOT NULL AND p.spy_price_365d IS NOT NULL
                   THEN p.amount_mid ELSE 0 END), 0) * 100 AS avg_1yr_alpha_pct,

    SUM(CASE WHEN p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
             AND p.spy_price_at_transaction IS NOT NULL AND p.spy_price_365d IS NOT NULL
             AND (p.price_365d - p.price_at_transaction) / p.price_at_transaction
                 > (p.spy_price_365d - p.spy_price_at_transaction) / p.spy_price_at_transaction
             THEN 1 ELSE 0 END) * 100.0
      / NULLIF(SUM(CASE WHEN p.price_at_transaction IS NOT NULL AND p.price_365d IS NOT NULL
                        AND p.spy_price_at_transaction IS NOT NULL AND p.spy_price_365d IS NOT NULL
                   THEN 1 ELSE 0 END), 0) AS alpha_win_rate_1yr_pct

FROM per_trade p
JOIN legislators l ON l.id = p.legislator_id
GROUP BY p.legislator_id, p.year;

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
