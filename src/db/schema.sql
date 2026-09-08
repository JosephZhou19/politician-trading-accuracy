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
    filing_date         TEXT,  -- nullable: a needs_ocr filing's real date genuinely isn't
                                -- known yet (e.g. House's scanned legacy paper forms give
                                -- no reliable machine-readable date) - a guessed value
                                -- would be worse than admitting we don't know
    source_url          TEXT NOT NULL,
    document_format     TEXT NOT NULL CHECK (document_format IN ('html', 'pdf', 'image')),
    raw_file_path       TEXT,
    raw_doc_hash        TEXT,
    fetched_at          TEXT NOT NULL,
    parsed_at           TEXT,
    parse_status        TEXT NOT NULL DEFAULT 'pending'
                             CHECK (parse_status IN ('pending', 'parsed', 'failed', 'needs_ocr')),
    -- nominal_date, superseded_by_filing_id: amendment reconciliation. Senate report titles
    -- say "for MM/DD/YYYY" - equal to filing_date for a normal filing, but for an amendment
    -- it's the date of the ORIGINAL being corrected (confirmed against a real amendment
    -- document - amendments carry no other reference to what they amend, not even the
    -- original's ID). Grouping filings by (legislator, nominal_date) clusters an original
    -- with all its amendments, since every amendment in a chain references the original's
    -- date, not the previous amendment's. NULL on House filings - no equivalent reference is
    -- exposed there, and no House PTR amendment has been observed to even exist (checked
    -- 2020-2026 live) so there's nothing to reconcile yet.
    nominal_date            TEXT,
    -- Precise "Filed MM/DD/YYYY @ H:MM AM/PM" timestamp from the report page itself, as
    -- "YYYY-MM-DDTHH:MM" (Senate only). Needed because filing_date alone (a plain date) is
    -- too coarse to order amendment chains correctly: confirmed on real data that three of
    -- Whitehouse's amendments were all filed on the identical calendar day (9:41 AM, 3:42 PM,
    -- 4:15 PM) - date-only ordering can't tell them apart. NULL for paper filings (no HTML
    -- fetched) and all House filings.
    filed_at                TEXT,
    -- The explicit sequence number from "(Amendment N)" in the report title, when present.
    -- Confirmed to run in ascending order of actual filing time (Whitehouse's Amendment
    -- 1/2/3 were filed 9:41am/3:42pm/4:15pm the same day) - a direct signal from the source
    -- itself, more authoritative than inferring order from filed_at. Older amendments just
    -- say "(Amendment)" with no number; NULL for those and for non-amendment filings.
    amendment_number        INTEGER,
    superseded_by_filing_id INTEGER REFERENCES filings (id),
    -- Free-text flag for anything reconciliation found but couldn't safely auto-resolve -
    -- e.g. an amendment whose nominal_date matches more than one original filing, so which
    -- one it corrects is genuinely undeterminable from the source data. NULL means clean.
    reconciliation_note     TEXT,
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
    -- Set by cross-filing overlap reconciliation (src/ingest/reconcile_overlapping_trades.py):
    -- two unrelated filings (no amendment link) can both report the exact same real
    -- transaction, e.g. one filing re-discloses a trade an earlier filing already covered
    -- as part of a wider batch. Superseding happens per-trade, not per-filing like
    -- amendments, since a filing with an overlap can still have other, genuinely unique
    -- trades that must stay active.
    superseded_by_trade_id INTEGER REFERENCES trades (id),
    UNIQUE (filing_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_trades_filing_id ON trades (filing_id);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_transaction_date ON trades (transaction_date);

-- One row per scraper invocation, for observability once ingestion runs unattended on a
-- schedule: when did it last run, how much was new, what failed.
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
