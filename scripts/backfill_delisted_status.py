"""One-time backfill: labels every ticker with zero historical price data anywhere
(confirmed dead/acquired/merged - see PLAN.md) as price_status='delisted' in ticker_prices.

Doesn't change the daily trickle job's behavior - those tickers were already excluded from
its queue by construction. This is purely so a query against ticker_prices gives a
complete, unambiguous answer for every ticker instead of silence for the ones never
checked. See models.backfill_delisted_status for the exact logic.

Usage:
    python -m scripts.backfill_delisted_status --db data/congress_trades.db
"""
import argparse
import sqlite3

from dotenv import load_dotenv

from src.db import models

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    count = models.backfill_delisted_status(conn)
    print(f"Marked {count} ticker(s) delisted (zero historical price data anywhere).")


if __name__ == "__main__":
    main()
