"""SQLite access layer for the congressional trading disclosure DB."""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class _TursoRow:
    """Mimics sqlite3.Row (index/name access, .keys()) - libsql returns plain tuples."""

    __slots__ = ("_columns", "_data")

    def __init__(self, columns, data):
        self._columns = columns
        self._data = data

    def __getitem__(self, key):
        return self._data[key] if isinstance(key, int) else self._data[self._columns.index(key)]

    def keys(self):
        return self._columns

    def __iter__(self):
        return iter(self._data)

    def __repr__(self):
        return f"TursoRow({dict(zip(self._columns, self._data))!r})"


class _TursoCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def _columns(self):
        return [d[0] for d in self._cursor.description]

    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else _TursoRow(self._columns(), row)

    def fetchall(self):
        columns = self._columns()
        return [_TursoRow(columns, row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        return self._cursor.lastrowid


# Substrings of the two known-recoverable Hrana stream failures: the session going stale
# between queries (e.g. a scraper busy downloading PDFs) and a server-side idle-transaction
# rollback (e.g. a slow run of external API calls - Finnhub - sitting inside one open,
# uncommitted batch for too long). Both are transient and safe to retry once on a fresh
# connection; anything else re-raises rather than silently retrying an unknown failure.
_RECOVERABLE_STREAM_ERRORS = ("stream not found", "was idle for too long")


class _TursoConnection:
    """Wraps a libsql connection with sqlite3.Row-shaped results, and reconnects once on a
    recoverable Hrana stream error - Turso's remote session can go stale or roll back an
    idle transaction, and the connection object doesn't recover from that on its own."""

    def __init__(self, url, token):
        self._url = url
        self._token = token
        self._conn = self._new_conn()
        self.row_factory = None

    def _new_conn(self):
        import libsql

        return libsql.connect(database=self._url, auth_token=self._token)

    def _with_reconnect(self, call):
        start = time.monotonic()
        try:
            result = call()
        except ValueError as e:
            if not any(marker in str(e) for marker in _RECOVERABLE_STREAM_ERRORS):
                raise
            logger.warning(
                "Turso stream error after %.0fms - reconnecting and retrying: %s",
                (time.monotonic() - start) * 1000, str(e)[:200],
            )
            self._conn = self._new_conn()
            result = call()
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > 500:
            logger.warning("Slow Turso call: %.0fms", elapsed_ms)
        return result

    def execute(self, sql, params=()):
        return _TursoCursor(self._with_reconnect(lambda: self._conn.execute(sql, params)))

    def executescript(self, script):
        return self._with_reconnect(lambda: self._conn.executescript(script))

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the DB and ensure the schema exists. Connects to Turso when
    TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are both set in the environment - db_path is
    ignored in that case. Otherwise opens/creates a local SQLite file at db_path."""
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        conn = _TursoConnection(turso_url, turso_token)
    else:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    return conn


_TRADES_ADDITIVE_COLUMNS = [
    ("filing_status", "TEXT"),
    ("superseded_by_trade_id", "INTEGER REFERENCES trades(id)"),
    ("reconciliation_note", "TEXT"),
]

# Retired in favor of ticker_daily_prices (full history, one source of truth instead of three
# overlapping ones) - dropped from any DB that still has them from before this migration.
_TRADES_REMOVED_COLUMNS = [
    "price_at_transaction", "price_at_notification",
    "price_30d", "price_90d", "price_180d", "price_365d",
]


# Retired along with the point-price columns/ticker_prices they read - schema.sql no longer
# creates these, but an existing DB (this one confirmed live: dropping a column SQLite says
# is "used" by a view raises, even with the view's own definition already gone from
# schema.sql) still has the OLD view objects sitting in sqlite_master until explicitly
# dropped. Must run BEFORE the DROP COLUMN below on every existing DB, production included.
_RETIRED_VIEWS = ("politician_ticker_positions", "politician_totals", "politician_yearly_activity")


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive/removed columns applied after a DB already existed - CREATE TABLE IF NOT
    EXISTS in schema.sql only creates missing tables, it doesn't retrofit columns onto one
    that's already there."""
    for view in _RETIRED_VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {view}")
    conn.commit()
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for name, coltype in _TRADES_ADDITIVE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {coltype}")
    for name in _TRADES_REMOVED_COLUMNS:
        if name in existing:
            conn.execute(f"ALTER TABLE trades DROP COLUMN {name}")
    conn.commit()
    # Retired wholesale, same reasoning as the trades columns above - current_price is now
    # just the latest row in ticker_daily_prices; delisting-tracking moved to the slimmer
    # ticker_status table (see schema.sql).
    conn.execute("DROP TABLE IF EXISTS ticker_prices")
    conn.commit()


@dataclass
class Legislator:
    id: int
    first_name: str
    last_name: str
    chamber: str
    filer_status: str


@dataclass
class Filing:
    id: int
    legislator_id: int
    chamber: str
    external_filing_id: str
    filing_type: str
    is_amendment: bool
    filing_date: Optional[str]
    source_url: str
    document_format: str
    raw_file_path: Optional[str]
    raw_doc_hash: Optional[str]
    fetched_at: str
    parsed_at: Optional[str]
    parse_status: str
    nominal_date: Optional[str]
    filed_at: Optional[str]
    amendment_number: Optional[int]
    superseded_by_filing_id: Optional[int]
    reconciliation_note: Optional[str]


@dataclass
class TickerStatus:
    ticker: str
    status: str
    zero_streak: int
    last_checked_at: Optional[str]


@dataclass
class Trade:
    id: int
    filing_id: int
    source_row_number: int
    ticker: Optional[str]
    asset_name: str
    asset_type: Optional[str]
    transaction_type: str
    transaction_date: str
    notification_date: str
    amount_low: int
    amount_high: Optional[int]
    owner: str
    comment: Optional[str]
    raw_row_text: Optional[str]
    filing_status: Optional[str]
    superseded_by_trade_id: Optional[int]
    reconciliation_note: Optional[str]


def _row_to_filing(row: sqlite3.Row) -> Filing:
    fields = {k: row[k] for k in row.keys()}
    fields["is_amendment"] = bool(fields["is_amendment"])
    return Filing(**fields)


def _row_to_trade(row: sqlite3.Row) -> Trade:
    return Trade(**{k: row[k] for k in row.keys()})


def get_or_create_legislator(
    conn: sqlite3.Connection,
    first_name: str,
    last_name: str,
    chamber: str,
    filer_status: str,
) -> int:
    """Look up a legislator by (first_name, last_name, chamber); insert if not found."""
    first_name = first_name.strip()
    last_name = last_name.strip()
    row = conn.execute(
        "SELECT id FROM legislators WHERE first_name = ? AND last_name = ? AND chamber = ?",
        (first_name, last_name, chamber),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO legislators (first_name, last_name, chamber, filer_status) "
        "VALUES (?, ?, ?, ?)",
        (first_name, last_name, chamber, filer_status),
    )
    conn.commit()
    return cur.lastrowid


def get_filing_by_external_id(
    conn: sqlite3.Connection, chamber: str, external_filing_id: str
) -> Optional[Filing]:
    """Look up a filing already ingested for this source doc, so a scraper can skip re-fetching."""
    row = conn.execute(
        "SELECT * FROM filings WHERE chamber = ? AND external_filing_id = ?",
        (chamber, external_filing_id),
    ).fetchone()
    return _row_to_filing(row) if row else None


def get_filing_statuses_by_chamber(
    conn: sqlite3.Connection, chamber: str
) -> dict[str, tuple[int, str]]:
    """Bulk (external_filing_id -> (id, parse_status)) lookup for an entire chamber, in one
    round trip. A scraper doing its usual per-filing_year (House) or per-filer-type (Senate)
    dedup check via get_filing_by_external_id pays one Turso round-trip per candidate filing
    it has *already* ingested - over a high-latency connection (e.g. GitHub Actions -> Turso,
    ~120ms/call observed) that alone was the dominant cost of a full run (~11,700 calls,
    ~20-25 minutes) even though each individual lookup is a cheap indexed SEARCH. Building
    this dict once per chamber and checking it in-memory instead collapses that to one call."""
    rows = conn.execute(
        "SELECT external_filing_id, id, parse_status FROM filings WHERE chamber = ?",
        (chamber,),
    ).fetchall()
    return {row["external_filing_id"]: (row["id"], row["parse_status"]) for row in rows}


def insert_filing(
    conn: sqlite3.Connection,
    *,
    legislator_id: int,
    chamber: str,
    external_filing_id: str,
    filing_type: str,
    is_amendment: bool,
    source_url: str,
    document_format: str,
    fetched_at: str,
    filing_date: Optional[str] = None,
    raw_file_path: Optional[str] = None,
    raw_doc_hash: Optional[str] = None,
    nominal_date: Optional[str] = None,
    filed_at: Optional[str] = None,
    amendment_number: Optional[int] = None,
) -> int:
    """Insert a new filing. Raises sqlite3.IntegrityError on a duplicate - callers should
    check get_filing_by_external_id first to decide whether to fetch/parse at all."""
    cur = conn.execute(
        """INSERT INTO filings (legislator_id, chamber, external_filing_id, filing_type,
                                 is_amendment, filing_date, source_url, document_format,
                                 raw_file_path, raw_doc_hash, fetched_at, nominal_date,
                                 filed_at, amendment_number)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            legislator_id,
            chamber,
            external_filing_id,
            filing_type,
            int(is_amendment),
            filing_date,
            source_url,
            document_format,
            raw_file_path,
            raw_doc_hash,
            fetched_at,
            nominal_date,
            filed_at,
            amendment_number,
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_superseded(conn: sqlite3.Connection, filing_id: int, superseded_by_filing_id: int) -> None:
    conn.execute(
        "UPDATE filings SET superseded_by_filing_id = ? WHERE id = ?",
        (superseded_by_filing_id, filing_id),
    )
    conn.commit()


def set_reconciliation_note(conn: sqlite3.Connection, filing_id: int, note: str) -> None:
    """Appends to any existing note rather than overwriting it - a filing can be flagged by
    more than one independent check (e.g. an ingest-time sanity check and a later amendment
    reconciliation pass), and the first flag shouldn't silently disappear. Skips appending a
    note that's already present, so a repeated run of an idempotent check doesn't grow it
    without bound."""
    existing = conn.execute(
        "SELECT reconciliation_note FROM filings WHERE id = ?", (filing_id,)
    ).fetchone()[0]
    if existing and note in existing:
        return
    combined = f"{existing} | {note}" if existing else note
    conn.execute("UPDATE filings SET reconciliation_note = ? WHERE id = ?", (combined, filing_id))
    conn.commit()


def set_filing_date(conn: sqlite3.Connection, filing_id: int, filing_date: str) -> None:
    conn.execute("UPDATE filings SET filing_date = ? WHERE id = ?", (filing_date, filing_id))
    conn.commit()


def update_filing_parse_status(
    conn: sqlite3.Connection,
    filing_id: int,
    parse_status: str,
    parsed_at: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE filings SET parse_status = ?, parsed_at = ? WHERE id = ?",
        (parse_status, parsed_at, filing_id),
    )
    conn.commit()


def insert_trade(
    conn: sqlite3.Connection,
    *,
    filing_id: int,
    source_row_number: int,
    asset_name: str,
    transaction_type: str,
    transaction_date: str,
    notification_date: str,
    amount_low: int,
    owner: str,
    ticker: Optional[str] = None,
    asset_type: Optional[str] = None,
    amount_high: Optional[int] = None,
    comment: Optional[str] = None,
    raw_row_text: Optional[str] = None,
    filing_status: Optional[str] = None,
) -> Optional[int]:
    """Insert a trade line; returns None instead of inserting if (filing_id,
    source_row_number) already exists (expected on a re-parse). Dedup is keyed on the
    source's own row position, not a composite of business fields, since two distinct
    transactions can be identical on every one. Checks explicitly rather than using
    INSERT OR IGNORE, which would also swallow a CHECK violation from bad data."""
    existing = conn.execute(
        "SELECT id FROM trades WHERE filing_id = ? AND source_row_number = ?",
        (filing_id, source_row_number),
    ).fetchone()
    if existing:
        return None
    cur = conn.execute(
        """INSERT INTO trades (filing_id, source_row_number, ticker, asset_name, asset_type,
                                transaction_type, transaction_date, notification_date,
                                amount_low, amount_high, owner, comment, raw_row_text,
                                filing_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            filing_id,
            source_row_number,
            ticker,
            asset_name,
            asset_type,
            transaction_type,
            transaction_date,
            notification_date,
            amount_low,
            amount_high,
            owner,
            comment,
            raw_row_text,
            filing_status,
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_trade_superseded(conn: sqlite3.Connection, trade_id: int, superseded_by_trade_id: int) -> None:
    conn.execute(
        "UPDATE trades SET superseded_by_trade_id = ? WHERE id = ?",
        (superseded_by_trade_id, trade_id),
    )
    conn.commit()


def set_trade_reconciliation_note(conn: sqlite3.Connection, trade_id: int, note: str, *, commit: bool = True) -> None:
    conn.execute("UPDATE trades SET reconciliation_note = ? WHERE id = ?", (note, trade_id))
    if commit:
        conn.commit()


def get_trades_for_filing(conn: sqlite3.Connection, filing_id: int) -> list[Trade]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE filing_id = ? ORDER BY id", (filing_id,)
    ).fetchall()
    return [_row_to_trade(row) for row in rows]


def delete_trades_for_filing(conn: sqlite3.Connection, filing_id: int) -> None:
    """Clear a filing's trades before re-parsing it (a retry reuses the existing filing
    row, so its old trades need clearing first)."""
    conn.execute("DELETE FROM trades WHERE filing_id = ?", (filing_id,))
    conn.commit()


# Consecutive daily misses required before concluding a ticker is actually delisted, not
# just mid trading-halt - anchored to SEC Rule 12(k), which caps an ordinary trading
# suspension at 10 business days, plus a margin.
ZERO_STREAK_DELIST_THRESHOLD = 15
# Once flagged delisted, how rarely to keep checking as a self-healing safety net (in case
# the classification above was ever wrong) rather than stopping forever.
DELISTED_RECHECK_DAYS = 30


def get_ticker_status(conn: sqlite3.Connection, ticker: str) -> Optional[TickerStatus]:
    row = conn.execute("SELECT * FROM ticker_status WHERE ticker = ?", (ticker,)).fetchone()
    return TickerStatus(**{k: row[k] for k in row.keys()}) if row else None


def record_ticker_seen(conn: sqlite3.Connection, ticker: str, checked_at: str, *, commit: bool = True) -> None:
    """A real price came back for this ticker this run (update_ticker_daily_prices.py) -
    always wins over any prior zero-streak, whether this is the ticker's first-ever
    successful check or a 'delisted' ticker unexpectedly trading again during its monthly
    safety-net check. commit=False lets a caller batch many tickers' writes into one
    round-trip instead of one per ticker."""
    conn.execute(
        """INSERT INTO ticker_status (ticker, status, zero_streak, last_checked_at)
           VALUES (?, 'active', 0, ?)
           ON CONFLICT (ticker) DO UPDATE SET
               status = 'active',
               zero_streak = 0,
               last_checked_at = excluded.last_checked_at""",
        (ticker, checked_at),
    )
    if commit:
        conn.commit()


def record_ticker_missed(conn: sqlite3.Connection, ticker: str, checked_at: str, *, commit: bool = True) -> None:
    """No usable price came back this run (update_ticker_daily_prices.py). Increments the
    streak in one statement (no read-then-write race) and flips to 'delisted' once the
    streak crosses ZERO_STREAK_DELIST_THRESHOLD. commit=False batches like
    record_ticker_seen."""
    conn.execute(
        """INSERT INTO ticker_status (ticker, status, zero_streak, last_checked_at)
           VALUES (?, 'active', 1, ?)
           ON CONFLICT (ticker) DO UPDATE SET
               zero_streak = ticker_status.zero_streak + 1,
               status = CASE WHEN ticker_status.zero_streak + 1 >= ?
                              THEN 'delisted' ELSE ticker_status.status END,
               last_checked_at = excluded.last_checked_at""",
        (ticker, checked_at, ZERO_STREAK_DELIST_THRESHOLD),
    )
    if commit:
        conn.commit()


def get_earliest_stock_trade_date(conn: sqlite3.Connection) -> Optional[str]:
    """Earliest transaction_date needing a benchmark price - drives how far back the
    first-ever SPY backfill needs to fetch, when benchmark_prices is still empty."""
    row = conn.execute(
        """SELECT MIN(transaction_date) AS d FROM trades
           WHERE asset_type IN ('ST', 'Stock') AND ticker IS NOT NULL AND ticker != ''"""
    ).fetchone()
    return row["d"] if row else None


def get_latest_benchmark_date(conn: sqlite3.Connection) -> Optional[str]:
    """Most recent date already in benchmark_prices - drives how far back a recurring SPY
    catch-up run needs to re-fetch (a small window, not the whole history)."""
    row = conn.execute("SELECT MAX(date) AS d FROM benchmark_prices").fetchone()
    return row["d"] if row else None


def set_benchmark_prices(conn: sqlite3.Connection, prices: list[tuple[str, float]]) -> None:
    """Bulk-loads (date, price) pairs into benchmark_prices in one batch commit - this is a
    one-time backfill of a few thousand rows, not a per-row recurring write."""
    for date, price in prices:
        conn.execute(
            """INSERT INTO benchmark_prices (date, price) VALUES (?, ?)
               ON CONFLICT (date) DO UPDATE SET price = excluded.price""",
            (date, price),
        )
    conn.commit()


def get_latest_sector_benchmark_date(conn: sqlite3.Connection, sector: str) -> Optional[str]:
    """Same as get_latest_benchmark_date, scoped to one sector's own series - each sector
    ETF backfill runs independently and needs its own catch-up window."""
    row = conn.execute(
        "SELECT MAX(date) AS d FROM sector_benchmark_prices WHERE sector = ?", (sector,)
    ).fetchone()
    return row["d"] if row else None


def set_sector_benchmark_prices(
    conn: sqlite3.Connection, sector: str, prices: list[tuple[str, float]]
) -> None:
    """Same as set_benchmark_prices, but for one sector's series in sector_benchmark_prices."""
    for date, price in prices:
        conn.execute(
            """INSERT INTO sector_benchmark_prices (sector, date, price) VALUES (?, ?, ?)
               ON CONFLICT (sector, date) DO UPDATE SET price = excluded.price""",
            (sector, date, price),
        )
    conn.commit()


def get_daily_price_backfill_cursor(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT last_ticker FROM daily_price_backfill_cursor WHERE id = 1").fetchone()
    return row["last_ticker"] if row else None


def set_daily_price_backfill_cursor(conn: sqlite3.Connection, last_ticker: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO daily_price_backfill_cursor (id, last_ticker) VALUES (1, ?)
           ON CONFLICT (id) DO UPDATE SET last_ticker = excluded.last_ticker""",
        (last_ticker,),
    )
    conn.commit()


def get_all_traded_tickers(conn: sqlite3.Connection) -> list[str]:
    """Every distinct ticker ever disclosed in a priceable (stock/option) trade - the
    universe backfill_ticker_daily_prices.py works through. Sorted so the resume-cursor
    comparison (ticker > cursor) in _rotate_to_resume_point-style logic is well-defined."""
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM trades
           WHERE ticker IS NOT NULL AND ticker != '' AND asset_type IN ('ST', 'Stock', 'OP')
           ORDER BY ticker"""
    ).fetchall()
    return [r["ticker"] for r in rows]


_TICKER_QUERY_BATCH_SIZE = 500


def filter_active_or_recheckable(conn: sqlite3.Connection, tickers: list[str]) -> list[str]:
    """Filters an arbitrary ticker list down to ones NOT currently 'delisted' in
    ticker_status, except when their once-a-month safety-net recheck (DELISTED_RECHECK_DAYS)
    is due. A ticker with no ticker_status row at all (never checked) always passes through."""
    if not tickers:
        return []
    statuses: dict[str, tuple[str, Optional[str]]] = {}
    for start in range(0, len(tickers), _TICKER_QUERY_BATCH_SIZE):
        batch = tickers[start:start + _TICKER_QUERY_BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT ticker, status, last_checked_at FROM ticker_status WHERE ticker IN ({placeholders})",
            batch,
        ).fetchall()
        statuses.update({r["ticker"]: (r["status"], r["last_checked_at"]) for r in rows})

    now = datetime.datetime.now(datetime.timezone.utc)
    result = []
    for ticker in tickers:
        status = statuses.get(ticker)
        if status is None:
            result.append(ticker)
            continue
        ticker_status, last_checked_at = status
        if ticker_status != "delisted":
            result.append(ticker)
            continue
        if last_checked_at is None or (now - datetime.datetime.fromisoformat(last_checked_at)).days >= DELISTED_RECHECK_DAYS:
            result.append(ticker)
    return result


def get_earliest_trade_dates_by_ticker(conn: sqlite3.Connection) -> dict[str, str]:
    """Every traded ticker's own earliest transaction_date, in one query - the start date
    backfill_ticker_daily_prices.py needs per ticker (each ticker's full history only goes
    back as far as its own first disclosed trade, not some shared universe-wide date)."""
    rows = conn.execute(
        """SELECT ticker, MIN(transaction_date) AS d FROM trades
           WHERE ticker IS NOT NULL AND ticker != '' AND asset_type IN ('ST', 'Stock', 'OP')
           GROUP BY ticker"""
    ).fetchall()
    return {r["ticker"]: datetime.date.fromisoformat(r["d"]) for r in rows}


def get_latest_daily_price_dates(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, str]:
    """Most recent date already stored in ticker_daily_prices for each of the given tickers -
    a ticker absent from the result has no stored history yet (needs a full backfill, not an
    incremental catch-up). Chunked to stay under SQLite/Turso's bound-parameter limit - the
    full ticker universe here (~3,500) can exceed it in one IN (...) - same reasoning as
    _refetch_by_id in sync_local_mirror.py."""
    result: dict[str, str] = {}
    for start in range(0, len(tickers), _TICKER_QUERY_BATCH_SIZE):
        batch = tickers[start:start + _TICKER_QUERY_BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"""SELECT ticker, MAX(date) AS d FROM ticker_daily_prices
                WHERE ticker IN ({placeholders}) GROUP BY ticker""",
            batch,
        ).fetchall()
        result.update({r["ticker"]: r["d"] for r in rows})
    return result


def get_ticker_daily_price(conn: sqlite3.Connection, ticker: str, date: str) -> Optional[float]:
    """Single stored price lookup - used by the incremental catch-up to sanity-check a
    freshly-fetched value against what's already on record for that same date (the
    split-detection check - see update_ticker_daily_prices.py)."""
    row = conn.execute(
        "SELECT price FROM ticker_daily_prices WHERE ticker = ? AND date = ?", (ticker, date)
    ).fetchone()
    return row["price"] if row else None


def set_ticker_daily_prices(
    conn: sqlite3.Connection, ticker: str, prices: list[tuple[str, float]], *, commit: bool = True
) -> None:
    """Bulk-loads (date, price) pairs for one ticker into ticker_daily_prices.
    commit=False lets a caller batch many tickers' writes into one round-trip instead of one
    per ticker."""
    for date, price in prices:
        conn.execute(
            """INSERT INTO ticker_daily_prices (ticker, date, price) VALUES (?, ?, ?)
               ON CONFLICT (ticker, date) DO UPDATE SET price = excluded.price""",
            (ticker, date, price),
        )
    if commit:
        conn.commit()


def replace_ticker_daily_prices(
    conn: sqlite3.Connection, ticker: str, prices: list[tuple[str, float]], *, commit: bool = True
) -> None:
    """Wipes and rewrites one ticker's ENTIRE stored history - used only when the
    incremental catch-up detects a split has moved the adjustment basis since the last
    full fetch (see update_ticker_daily_prices.py's _needs_rebase). A plain upsert of just
    the new window would otherwise leave the old rows on the pre-split basis forever."""
    conn.execute("DELETE FROM ticker_daily_prices WHERE ticker = ?", (ticker,))
    set_ticker_daily_prices(conn, ticker, prices, commit=commit)


def backfill_delisted_status(conn: sqlite3.Connection) -> int:
    """One-time labeling pass: gives every ticker with zero rows in ticker_daily_prices an
    explicit status='delisted' row instead of leaving it absent from ticker_status - absence
    reads as ambiguous ("confirmed dead" vs. "not checked yet") for downstream analysis.
    zero_streak is set to the threshold for consistency, but last_checked_at stays NULL since
    no live check happened - this comes from the historical backfill's absence of data, a
    different source. Returns the count newly labeled."""
    rows = conn.execute(
        """
        SELECT DISTINCT t.ticker FROM trades t
        WHERE t.ticker IS NOT NULL AND t.ticker != ''
          AND NOT EXISTS (SELECT 1 FROM ticker_daily_prices tdp WHERE tdp.ticker = t.ticker)
          AND NOT EXISTS (SELECT 1 FROM ticker_status ts WHERE ts.ticker = t.ticker)
        """
    ).fetchall()
    tickers = [row["ticker"] for row in rows]
    for ticker in tickers:
        conn.execute(
            """INSERT INTO ticker_status (ticker, status, zero_streak, last_checked_at)
               VALUES (?, 'delisted', ?, NULL)""",
            (ticker, ZERO_STREAK_DELIST_THRESHOLD),
        )
    conn.commit()
    return len(tickers)


def start_ingestion_run(conn: sqlite3.Connection, chamber: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingestion_runs (chamber, started_at, status) VALUES (?, ?, 'running')",
        (chamber, started_at),
    )
    conn.commit()
    return cur.lastrowid


def finish_ingestion_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    filings_found: int,
    filings_new: int,
    filings_failed: int,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    conn.execute(
        """UPDATE ingestion_runs
           SET finished_at = ?, filings_found = ?, filings_new = ?, filings_failed = ?,
               status = ?, error_message = ?
           WHERE id = ?""",
        (finished_at, filings_found, filings_new, filings_failed, status, error_message, run_id),
    )
    conn.commit()
