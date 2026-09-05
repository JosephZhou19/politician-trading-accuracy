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
    return conn


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
    filing_date: str
    source_url: str
    document_format: str
    raw_file_path: Optional[str]
    raw_doc_hash: Optional[str]
    fetched_at: str
    parsed_at: Optional[str]
    parse_status: str


@dataclass
class Trade:
    id: int
    filing_id: int
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
    """Look up a legislator by (first_name, last_name, chamber); insert if not found.

    This is the only identity key the source data actually gives us - see PLAN.md.
    """
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
    filing_date: str,
    source_url: str,
    document_format: str,
    fetched_at: str,
    raw_file_path: Optional[str] = None,
    raw_doc_hash: Optional[str] = None,
) -> int:
    """Insert a new filing. Callers should check get_filing_by_external_id first -
    this raises sqlite3.IntegrityError on a duplicate (chamber, external_filing_id)
    rather than silently ignoring it, since that would indicate the caller skipped
    the existence check it needed to decide whether to fetch/parse this document at all.
    """
    cur = conn.execute(
        """INSERT INTO filings (legislator_id, chamber, external_filing_id, filing_type,
                                 is_amendment, filing_date, source_url, document_format,
                                 raw_file_path, raw_doc_hash, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        ),
    )
    conn.commit()
    return cur.lastrowid


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
) -> Optional[int]:
    """Insert a trade line, checked against the natural key (filing_id, asset_name,
    transaction_date, transaction_type, amount_low, owner) since re-parsing the same filing
    is expected to hit the same rows again - that's not a bug the way a duplicate filing
    would be. Returns None if the row already existed instead of being inserted.

    This deliberately does NOT use "INSERT OR IGNORE": that suppresses every constraint
    violation, not just the duplicate-key one, which would silently swallow a bad
    transaction_type/owner value from a parser bug instead of raising. Checking for the
    duplicate explicitly first, then doing a plain INSERT, keeps CHECK violations loud.
    """
    existing = conn.execute(
        """SELECT id FROM trades
           WHERE filing_id = ? AND asset_name = ? AND transaction_date = ?
             AND transaction_type = ? AND amount_low = ? AND owner = ?""",
        (filing_id, asset_name, transaction_date, transaction_type, amount_low, owner),
    ).fetchone()
    if existing:
        return None
    cur = conn.execute(
        """INSERT INTO trades (filing_id, ticker, asset_name, asset_type,
                                transaction_type, transaction_date, notification_date,
                                amount_low, amount_high, owner, comment, raw_row_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            filing_id,
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
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_trades_for_filing(conn: sqlite3.Connection, filing_id: int) -> list[Trade]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE filing_id = ? ORDER BY id", (filing_id,)
    ).fetchall()
    return [_row_to_trade(row) for row in rows]
