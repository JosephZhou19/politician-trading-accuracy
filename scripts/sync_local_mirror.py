"""Incremental sync of a local sqlite mirror from Turso, so exploring analytics locally
doesn't repeatedly pay a full-table rows-read for tables that barely changed.

Two sync strategies per table, chosen by whether existing rows can change after insert:
- Append-only (legislators, benchmark_prices): new rows only, `id`/`date` strictly greater
  than the local file's own current max. Correct, not just an optimization, because
  nothing ever updates an existing row in these tables after insert.
- filings/trades: new rows the same way, PLUS a targeted re-fetch (by id, a cheap indexed
  lookup) of whatever's still locally "pending" - the only existing rows that can change
  later (price backfills arriving over the following year, parse_status flipping from
  pending/failed/needs_ocr to parsed - the last of those via the OCR human-review pass in
  scripts/review_ocr_drafts.py, not just the automated parsers - amendment reconciliation
  setting superseded_by_trade_id/note). "Pending" is computed against the LOCAL file (free)
  using the exact same definition the real pipeline uses, so it matches production instead
  of being independently defined: models.get_trades_needing_prices for trades, parse_status
  != 'parsed' for filings.
- Small tables (ticker_prices, trickle_cursor, ingestion_runs): always fully re-fetched.
  Each is small enough (<5k rows) that incremental logic isn't worth the complexity, and
  ticker_prices genuinely changes for most of its rows every single day anyway.

Known gap, accepted rather than engineered around: a reconciliation event on a trade/filing
that's already fully priced/parsed (e.g. a very late-arriving amendment correcting an old,
already-settled trade) won't be caught until that row happens to reappear in a "pending"
check for some other reason, or a full re-export is run. Rare in practice (a handful of
ambiguous/no-match cases per ingest run, per PLAN.md) and this is an exploration mirror,
not a system of record - not worth the complexity of a proper updated_at column for every
write path just to close this gap.

Usage:
    python -m scripts.sync_local_mirror --db data/congress_trades.db
"""
import argparse
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from src.db import models

load_dotenv()

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"
SMALL_TABLES = ["ticker_prices", "trickle_cursor", "ingestion_runs"]


def _connect_turso():
    """Refuses to proceed if Turso credentials aren't loaded, rather than letting
    models.connect() silently fall back to a local file - which here would mean syncing
    a file from itself. See PLAN.md's load_dotenv() search-path gotcha."""
    if not (os.environ.get("TURSO_DATABASE_URL") and os.environ.get("TURSO_AUTH_TOKEN")):
        raise RuntimeError(
            "TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set - refusing to sync, since "
            "models.connect() would otherwise open the same local file as both source "
            "and target."
        )
    return models.connect("unused-since-turso-env-is-set")


def _connect_local(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    models._migrate(conn)
    return conn


def _replace_rows(local, table, rows):
    if not rows:
        return 0
    columns = rows[0].keys()
    placeholders = ",".join("?" * len(columns))
    col_list = ",".join(columns)
    local.executemany(
        f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
        [tuple(r[c] for c in columns) for r in rows],
    )
    return len(rows)


def sync_append_only(turso, local, table, key_column):
    """Pulls only rows newer than the local file's own current max key - correct as long
    as the caller confirms this table never updates an existing row after insert."""
    local_max = local.execute(f"SELECT MAX({key_column}) FROM {table}").fetchone()[0]
    if local_max is None:
        rows = turso.execute(f"SELECT * FROM {table}").fetchall()
    else:
        rows = turso.execute(
            f"SELECT * FROM {table} WHERE {key_column} > ?", (local_max,)
        ).fetchall()
    return _replace_rows(local, table, rows)


_REFETCH_BATCH_SIZE = 500


def _refetch_by_id(turso, local, table, ids):
    """Batched to stay under Turso/SQLite's bound-parameter limit - a "pending" set can
    easily exceed it (e.g. every trade still awaiting a price-horizon backfill)."""
    total = 0
    for start in range(0, len(ids), _REFETCH_BATCH_SIZE):
        batch = ids[start:start + _REFETCH_BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = turso.execute(
            f"SELECT * FROM {table} WHERE id IN ({placeholders})", batch
        ).fetchall()
        total += _replace_rows(local, table, rows)
    return total


def sync_filings(turso, local):
    new_count = sync_append_only(turso, local, "filings", "id")
    pending_ids = [
        row["id"] for row in local.execute(
            "SELECT id FROM filings WHERE parse_status != 'parsed'"
        ).fetchall()
    ]
    return new_count, _refetch_by_id(turso, local, "filings", pending_ids)


def sync_trades(turso, local):
    new_count = sync_append_only(turso, local, "trades", "id")
    pending_ids = [t.id for t in models.get_trades_needing_prices(local)]
    return new_count, _refetch_by_id(turso, local, "trades", pending_ids)


def sync_small_table(turso, local, table):
    """Small enough (<5k rows) that incremental logic isn't worth it - also correctly
    picks up any row deleted upstream, which an id-based sync never would."""
    rows = turso.execute(f"SELECT * FROM {table}").fetchall()
    local.execute(f"DELETE FROM {table}")
    return _replace_rows(local, table, rows)


def sync(db_path):
    turso = _connect_turso()
    local = _connect_local(db_path)
    local.execute("PRAGMA foreign_keys = OFF")

    summary = {"legislators_new": sync_append_only(turso, local, "legislators", "id")}
    summary["filings_new"], summary["filings_rechecked"] = sync_filings(turso, local)
    summary["trades_new"], summary["trades_rechecked"] = sync_trades(turso, local)
    summary["benchmark_prices_new"] = sync_append_only(turso, local, "benchmark_prices", "date")
    for table in SMALL_TABLES:
        summary[f"{table}_refreshed"] = sync_small_table(turso, local, table)

    local.execute("PRAGMA foreign_keys = ON")
    local.commit()
    local.close()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    args = parser.parse_args()
    print(sync(args.db))


if __name__ == "__main__":
    main()
