"""SQLite access layer for the congressional trading disclosure DB."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite DB at db_path and ensure the schema exists."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive columns added after a DB already existed - CREATE TABLE IF NOT EXISTS in
    schema.sql only creates missing tables, it doesn't retrofit columns onto one that's
    already there."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    if "filing_status" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN filing_status TEXT")
    if "superseded_by_trade_id" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN superseded_by_trade_id INTEGER REFERENCES trades(id)")
    if "reconciliation_note" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN reconciliation_note TEXT")
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
    conn.execute("UPDATE filings SET reconciliation_note = ? WHERE id = ?", (note, filing_id))
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


def set_trade_reconciliation_note(conn: sqlite3.Connection, trade_id: int, note: str) -> None:
    conn.execute("UPDATE trades SET reconciliation_note = ? WHERE id = ?", (note, trade_id))
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
