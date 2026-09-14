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


class _TursoConnection:
    """Wraps a libsql connection with sqlite3.Row-shaped results, and reconnects once on a
    Hrana "stream not found" error - Turso's remote session can go stale if enough wall-clock
    time passes between queries (e.g. a scraper busy downloading PDFs), and the connection
    object doesn't recover from that on its own."""

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
            if "stream not found" not in str(e):
                raise
            logger.warning(
                "Turso stream went stale after %.0fms - reconnecting and retrying",
                (time.monotonic() - start) * 1000,
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
    ("price_at_transaction", "REAL"),
    ("price_at_notification", "REAL"),
    ("price_30d", "REAL"),
    ("price_90d", "REAL"),
    ("price_180d", "REAL"),
    ("price_365d", "REAL"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive columns added after a DB already existed - CREATE TABLE IF NOT EXISTS in
    schema.sql only creates missing tables, it doesn't retrofit columns onto one that's
    already there."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for name, coltype in _TRADES_ADDITIVE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {coltype}")
    conn.commit()
    _migrate_ticker_prices(conn)


def _migrate_ticker_prices(conn: sqlite3.Connection) -> None:
    """ticker_prices originally had NOT NULL current_price/price_updated_at, before the
    price-trickle job's zero_streak tracking needed to represent "no real price seen yet."
    SQLite can't drop a NOT NULL constraint via ALTER, so this drops and recreates the table -
    safe because it was created but never populated (no trickle job existed yet to write to
    it) as of this migration."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(ticker_prices)")}
    if not existing or "price_status" in existing:
        return
    conn.execute("DROP TABLE ticker_prices")
    conn.executescript(
        """CREATE TABLE ticker_prices (
               ticker            TEXT PRIMARY KEY,
               current_price     REAL,
               price_updated_at  TEXT,
               price_status      TEXT NOT NULL DEFAULT 'active' CHECK (price_status IN ('active', 'delisted')),
               zero_streak       INTEGER NOT NULL DEFAULT 0,
               last_checked_at   TEXT
           );"""
    )
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
class TickerPrice:
    ticker: str
    current_price: Optional[float]
    price_updated_at: Optional[str]
    price_status: str
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
    price_at_transaction: Optional[float]
    price_at_notification: Optional[float]
    price_30d: Optional[float]
    price_90d: Optional[float]
    price_180d: Optional[float]
    price_365d: Optional[float]


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


PRICE_COLUMNS = (
    "price_at_transaction", "price_at_notification",
    "price_30d", "price_90d", "price_180d", "price_365d",
)
# Trading-day offset from transaction_date for each horizon column, in the order a trade's
# journey through them actually happens - used by the daily catch-up job to know which date
# to test each column against.
HORIZON_COLUMNS = (("price_30d", 30), ("price_90d", 90), ("price_180d", 180), ("price_365d", 365))


def set_trade_prices(conn: sqlite3.Connection, trade_id: int, prices: dict, *, commit: bool = True) -> None:
    """Sets one or more price columns on a trade in a single UPDATE - a ticker with
    thousands of trades (e.g. MSFT) would otherwise mean up to 6 separate network
    round-trips per trade against Turso just to set that trade's own prices. commit=False
    lets a caller batch many trades' worth of writes into one commit (e.g. per ticker group)
    instead of one round-trip per trade."""
    bad = set(prices) - set(PRICE_COLUMNS)
    if bad:
        raise ValueError(f"not price column(s): {bad}")
    if not prices:
        return
    assignments = ", ".join(f"{col} = ?" for col in prices)
    conn.execute(f"UPDATE trades SET {assignments} WHERE id = ?", (*prices.values(), trade_id))
    if commit:
        conn.commit()


def get_trades_needing_prices(conn: sqlite3.Connection, today: Optional[str] = None) -> list[Trade]:
    """Ticker'd trades with at least one currently-fetchable price still unset: transaction/
    notification price (always fetchable - both dates are already in the past by the time a
    trade exists at all), or a 30/90/180/365-day horizon whose date has now arrived. This is
    the shared work queue for both the one-time historical backfill and the daily catch-up
    job - the same trade can reappear here across several days as later horizons arrive.

    today defaults to Python's local date, not SQLite's date('now') (which is UTC) -
    confirmed these disagree for several hours every evening in US time zones, which was
    silently inflating this queue with trades whose horizon looked arrived here but wasn't
    once backfill_prices.py's own (local-time) date check ran, wasting a real yfinance
    fetch for no benefit. Also excludes tickers already confirmed permanently delisted
    (backfill_delisted_status) - retrying a ticker with zero yfinance data forever wastes a
    fetch on every single run for no possible benefit."""
    if today is None:
        today = datetime.date.today().isoformat()
    horizon_conditions = " OR ".join(
        f"({col} IS NULL AND date(transaction_date, '+{days} days') <= date(?))"
        for col, days in HORIZON_COLUMNS
    )
    params = [today] * len(HORIZON_COLUMNS)
    rows = conn.execute(
        f"""SELECT * FROM trades t WHERE t.ticker IS NOT NULL AND t.ticker != '' AND (
                t.price_at_transaction IS NULL OR t.price_at_notification IS NULL
                OR {horizon_conditions}
            )
            AND NOT EXISTS (
                SELECT 1 FROM ticker_prices tp WHERE tp.ticker = t.ticker AND tp.price_status = 'delisted'
            )""",
        params,
    ).fetchall()
    return [_row_to_trade(row) for row in rows]


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


# Consecutive daily zero-responses from Finnhub required before concluding a ticker is
# actually delisted, not just mid trading-halt - anchored to SEC Rule 12(k), which caps an
# ordinary trading suspension at 10 business days, plus a margin.
ZERO_STREAK_DELIST_THRESHOLD = 15
# Once flagged delisted, how rarely to keep checking as a self-healing safety net (in case
# the classification above was ever wrong) rather than stopping forever.
DELISTED_RECHECK_DAYS = 30


def get_ticker_price(conn: sqlite3.Connection, ticker: str) -> Optional[TickerPrice]:
    row = conn.execute("SELECT * FROM ticker_prices WHERE ticker = ?", (ticker,)).fetchone()
    return TickerPrice(**{k: row[k] for k in row.keys()}) if row else None


def record_real_price(conn: sqlite3.Connection, ticker: str, price: float, checked_at: str, *, commit: bool = True) -> None:
    """A real Finnhub quote came back - always wins over any prior zero-streak, whether
    this is the ticker's first-ever price or a 'delisted' ticker unexpectedly trading again
    during its monthly safety-net check. commit=False lets a caller batch many tickers'
    writes into one round-trip instead of one per ticker."""
    conn.execute(
        """INSERT INTO ticker_prices (ticker, current_price, price_updated_at, price_status, zero_streak, last_checked_at)
           VALUES (?, ?, ?, 'active', 0, ?)
           ON CONFLICT (ticker) DO UPDATE SET
               current_price = excluded.current_price,
               price_updated_at = excluded.price_updated_at,
               price_status = 'active',
               zero_streak = 0,
               last_checked_at = excluded.last_checked_at""",
        (ticker, price, checked_at, checked_at),
    )
    if commit:
        conn.commit()


def record_zero_response(conn: sqlite3.Connection, ticker: str, checked_at: str, *, commit: bool = True) -> None:
    """Finnhub returned c == 0 (no data) - never overwrites current_price, which stays
    frozen at its last real value (or NULL, if this ticker has never had one). Increments
    the streak in one statement (no read-then-write race) and flips to 'delisted' once the
    streak crosses ZERO_STREAK_DELIST_THRESHOLD. commit=False batches like record_real_price."""
    conn.execute(
        """INSERT INTO ticker_prices (ticker, price_status, zero_streak, last_checked_at)
           VALUES (?, 'active', 1, ?)
           ON CONFLICT (ticker) DO UPDATE SET
               zero_streak = ticker_prices.zero_streak + 1,
               price_status = CASE WHEN ticker_prices.zero_streak + 1 >= ?
                                    THEN 'delisted' ELSE ticker_prices.price_status END,
               last_checked_at = excluded.last_checked_at""",
        (ticker, checked_at, ZERO_STREAK_DELIST_THRESHOLD),
    )
    if commit:
        conn.commit()


def get_tickers_due_for_price_check(conn: sqlite3.Connection) -> list[str]:
    """The daily trickle job's work queue: every ticker that has at least one real
    historical price already on some trade (a ticker with zero price data anywhere is
    already known-dead from the one-time backfill - Finnhub returning c=0 for it would be
    known information, not worth spending quota to reconfirm), joined against ticker_prices
    to skip 'delisted' tickers except on their once-a-month safety-net recheck. Ordered by
    ticker so the trickle job's resume cursor (a bookmark by ticker value) is deterministic
    across runs even as the underlying set of due tickers shifts."""
    # priced_tickers is computed once (a single pass over trades) rather than as a
    # correlated EXISTS re-evaluated per trade row - see PLAN.md.
    rows = conn.execute(
        f"""
        WITH priced_tickers AS (
            SELECT DISTINCT ticker FROM trades
            WHERE ticker IS NOT NULL AND ticker != ''
              AND (price_at_transaction IS NOT NULL OR price_at_notification IS NOT NULL
                   OR price_30d IS NOT NULL OR price_90d IS NOT NULL
                   OR price_180d IS NOT NULL OR price_365d IS NOT NULL)
        )
        SELECT pt.ticker FROM priced_tickers pt
        LEFT JOIN ticker_prices tp ON tp.ticker = pt.ticker
        WHERE tp.ticker IS NULL
           OR tp.price_status = 'active'
           OR (tp.price_status = 'delisted'
               AND (tp.last_checked_at IS NULL
                    OR julianday('now') - julianday(tp.last_checked_at) >= {DELISTED_RECHECK_DAYS}))
        ORDER BY pt.ticker
        """
    ).fetchall()
    return [row["ticker"] for row in rows]


def get_trickle_cursor(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT last_ticker FROM trickle_cursor WHERE id = 1").fetchone()
    return row["last_ticker"] if row else None


def set_trickle_cursor(conn: sqlite3.Connection, last_ticker: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO trickle_cursor (id, last_ticker) VALUES (1, ?)
           ON CONFLICT (id) DO UPDATE SET last_ticker = excluded.last_ticker""",
        (last_ticker,),
    )
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


def backfill_delisted_status(conn: sqlite3.Connection) -> int:
    """One-time labeling pass: gives every ticker with zero historical price data anywhere
    (already excluded from get_tickers_due_for_price_check's queue by construction) an
    explicit price_status='delisted' row instead of leaving it absent from ticker_prices -
    absence reads as ambiguous ("confirmed dead" vs. "not checked yet") for downstream
    analysis. zero_streak is set to the threshold for consistency, but last_checked_at
    stays NULL since no Finnhub call happened - this comes from the historical backfill's
    absence of data, a different source. Returns the count newly labeled."""
    # Same fix as get_tickers_due_for_price_check: compute has-any-price per ticker in one
    # grouped pass instead of a correlated EXISTS re-evaluated per trade row.
    rows = conn.execute(
        """
        WITH ticker_has_price AS (
            SELECT ticker,
                   MAX(price_at_transaction IS NOT NULL OR price_at_notification IS NOT NULL
                       OR price_30d IS NOT NULL OR price_90d IS NOT NULL
                       OR price_180d IS NOT NULL OR price_365d IS NOT NULL) AS has_price
            FROM trades
            WHERE ticker IS NOT NULL AND ticker != ''
            GROUP BY ticker
        )
        SELECT ticker FROM ticker_has_price
        WHERE has_price = 0 AND ticker NOT IN (SELECT ticker FROM ticker_prices)
        """
    ).fetchall()
    tickers = [row["ticker"] for row in rows]
    for ticker in tickers:
        conn.execute(
            """INSERT INTO ticker_prices (ticker, current_price, price_updated_at, price_status, zero_streak, last_checked_at)
               VALUES (?, NULL, NULL, 'delisted', ?, NULL)""",
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
