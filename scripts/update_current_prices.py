"""Daily trickle job: refreshes ticker_prices.current_price via Finnhub for every ticker
that has at least one real historical price already backfilled. A ticker with zero price
data anywhere is already known-dead from the one-time backfill - see
models.get_tickers_due_for_price_check for why those are excluded outright.

Handles a ticker going through M&A/delisting while under active tracking: Finnhub's
/quote returns c == 0 (not an error) for a dead symbol, indistinguishable in shape from a
fake ticker. This never overwrites current_price with a zero - see
models.record_zero_response for the streak-tracking and eventual 'delisted' downgrade.

Usage:
    python -m scripts.update_current_prices --db data/congress_trades.db
"""
import argparse
import datetime
import sqlite3

from dotenv import load_dotenv

from src.db import models
from src.market.current_price import fetch_current_prices

load_dotenv()


def update_current_prices(conn, ticker_limit=None):
    tickers = models.get_tickers_due_for_price_check(conn)
    if ticker_limit is not None:
        tickers = tickers[:ticker_limit]

    summary = {"tickers_checked": 0, "prices_updated": 0, "zero_responses": 0, "newly_delisted": 0}
    print(f"{len(tickers)} ticker(s) due for a price check.")

    for ticker, price in fetch_current_prices(tickers):
        checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        summary["tickers_checked"] += 1

        if price is not None:
            models.record_real_price(conn, ticker, price, checked_at)
            summary["prices_updated"] += 1
            continue

        existing = models.get_ticker_price(conn, ticker)
        old_streak = existing.zero_streak if existing else 0
        was_active = existing is None or existing.price_status == "active"
        models.record_zero_response(conn, ticker, checked_at)
        summary["zero_responses"] += 1
        if was_active and old_streak + 1 >= models.ZERO_STREAK_DELIST_THRESHOLD:
            print(f"{ticker}: zero-streak hit {models.ZERO_STREAK_DELIST_THRESHOLD} - "
                  f"flagging delisted, dropping to a once-every-{models.DELISTED_RECHECK_DAYS}-day check.")
            summary["newly_delisted"] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    parser.add_argument("--ticker-limit", type=int, default=None, help="Check at most N tickers (for testing)")
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = update_current_prices(conn, ticker_limit=args.ticker_limit)
    print(f"\nDone: {summary}")


if __name__ == "__main__":
    main()
