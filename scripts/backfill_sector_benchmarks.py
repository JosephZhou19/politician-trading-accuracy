"""Loads each SPDR Select Sector ETF's daily Open price history into sector_benchmark_prices,
one series per GICS sector - see src/market/sectors.py for the sector->ETF mapping. Same
design as backfill_spy_benchmark.py, just repeated per sector (11 yfinance calls instead of
one): incremental by design, safe to run daily, re-fetching only a small overlap window once
a sector's series already has data.

Usage:
    python -m scripts.backfill_sector_benchmarks --db data/congress_trades.db
"""
import argparse
import datetime
import sqlite3

from dotenv import load_dotenv

from src.db import models
from src.market.prices import fetch_ticker_history
from src.market.sectors import SECTOR_ETFS

load_dotenv()

# Same overlap window as backfill_spy_benchmark.py, for the same reason: cheap insurance
# against the most recent stored day needing a correction.
OVERLAP_DAYS = 5


def backfill_sector_benchmarks(conn):
    summary = {}
    for sector, etf in SECTOR_ETFS.items():
        latest = models.get_latest_sector_benchmark_date(conn, sector)
        if latest is not None:
            start_date = datetime.date.fromisoformat(latest) - datetime.timedelta(days=OVERLAP_DAYS)
        else:
            earliest = models.get_earliest_stock_trade_date(conn)
            if earliest is None:
                print("No stock trades found - nothing to backfill.")
                return summary
            start_date = datetime.date.fromisoformat(earliest)

        today = datetime.date.today()
        history = fetch_ticker_history(etf, start_date, today)
        if history is None:
            raise RuntimeError(f"yfinance returned no data for {etf} ({sector}) - can't proceed.")

        prices = [(date.isoformat(), price) for date, price in history.daily_prices()]
        models.set_sector_benchmark_prices(conn, sector, prices)
        summary[sector] = len(prices)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = backfill_sector_benchmarks(conn)
    print(f"Loaded day(s) into sector_benchmark_prices: {summary}")


if __name__ == "__main__":
    main()
