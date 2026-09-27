"""One-time (not part of any recurring job): pushes a local mirror's ENTIRE
ticker_daily_prices table to production Turso in large batched INSERT statements.

Exists specifically to pair with `backfill_ticker_daily_prices.py --local`: running the
heavy one-time backfill against a local SQLite file avoids per-row Turso round-trip latency
during the (already slow, yfinance-bound) fetch loop - confirmed live this session, Turso
round-trips on this instance can occasionally take 60-120+ seconds each, and
set_ticker_daily_prices does one execute() per row, so a ticker with a full multi-year
history could cost hundreds of individual round-trips. Pushing the whole result in one
bulk pass afterward (via models.bulk_insert_ticker_daily_prices, batch_size rows per
statement) cuts millions of potential round-trips down to a few thousand.

Ongoing updates after this one-time push go back to normal: update_ticker_daily_prices.py
via GitHub Actions, writing directly to Turso as today - that job's per-run row count is
small enough that this local-then-push detour isn't needed there.

Usage:
    python -m scripts.push_ticker_daily_prices_to_turso --local-db data/congress_trades.db
"""
import argparse
import time

from dotenv import load_dotenv

from src.db import models
from scripts.sync_local_mirror import _connect_turso

load_dotenv()

BATCH_SIZE = 500


def push_ticker_daily_prices(local_conn, turso_conn, batch_size=BATCH_SIZE):
    rows = local_conn.execute("SELECT ticker, date, price FROM ticker_daily_prices").fetchall()
    triples = [(r["ticker"], r["date"], r["price"]) for r in rows]
    print(f"{len(triples)} row(s) to push, {batch_size} per Turso round-trip "
          f"(~{-(-len(triples) // batch_size)} round-trip(s) total).")

    start = time.monotonic()
    models.bulk_insert_ticker_daily_prices(turso_conn, triples, batch_size=batch_size)
    elapsed = time.monotonic() - start
    print(f"Done in {elapsed:.0f}s.")
    return len(triples)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local-db", default="data/congress_trades.db")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    local_conn = models.connect_local(args.local_db)
    # Refuses to proceed if Turso credentials aren't loaded, rather than letting
    # models.connect() silently fall back to a local file - same gotcha
    # sync_local_mirror.py's own _connect_turso guards against.
    turso_conn = _connect_turso()

    count = push_ticker_daily_prices(local_conn, turso_conn, batch_size=args.batch_size)
    print(f"\nPushed {count} row(s) to Turso.")


if __name__ == "__main__":
    main()
