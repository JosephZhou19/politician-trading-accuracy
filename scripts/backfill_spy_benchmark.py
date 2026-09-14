"""One-time backfill: loads SPY's daily Open price history into benchmark_prices, so the
analytics views can compute alpha vs. the S&P 500 via a cheap indexed lookup instead of
duplicating a benchmark price onto every trade row. See PLAN.md for the full design
reasoning (why SPY over the raw index, why a lookup table over per-trade columns).

One yfinance call for SPY's whole history - not one per trade, not one per ticker.

Usage:
    python -m scripts.backfill_spy_benchmark --db data/congress_trades.db
"""
import argparse
import datetime
import sqlite3

from dotenv import load_dotenv

from src.db import models
from src.market.prices import fetch_ticker_history

load_dotenv()


def backfill_spy_benchmark(conn):
    earliest = models.get_earliest_stock_trade_date(conn)
    if earliest is None:
        print("No stock trades found - nothing to backfill.")
        return 0

    start_date = datetime.date.fromisoformat(earliest)
    today = datetime.date.today()
    history = fetch_ticker_history("SPY", start_date, today)
    if history is None:
        raise RuntimeError("yfinance returned no data for SPY - can't proceed without a benchmark.")

    prices = [(date.isoformat(), price) for date, price in history.daily_prices()]
    models.set_benchmark_prices(conn, prices)
    return len(prices)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    count = backfill_spy_benchmark(conn)
    print(f"Loaded {count} day(s) of SPY prices into benchmark_prices.")


if __name__ == "__main__":
    main()
