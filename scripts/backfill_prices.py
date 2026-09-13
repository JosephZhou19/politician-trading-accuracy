"""Fills in trades' price_at_transaction/price_at_notification/price_30d/90d/180d/365d
columns via yfinance. Groups trades by ticker so each ticker's full needed date range is
fetched once, regardless of how many trades or price columns draw from it.

Shared between two uses, both safe to interrupt and re-run (a trade only appears in the
work queue while at least one of its currently-fetchable price columns is still unset):
  - The one-time historical backfill for the existing trade backlog.
  - The daily catch-up job, which finds whatever 30/90/180/365-day horizons have newly
    arrived since the last run.

Usage:
    python -m scripts.backfill_prices --db data/congress_trades.db
"""
import argparse
import datetime
import sqlite3
import time
from collections import defaultdict

from dotenv import load_dotenv

from src.db import models
from src.market.prices import fetch_ticker_history, fetch_ticker_history_stockanalysis

load_dotenv()

# Seconds to wait between per-ticker yfinance calls - a cheap courtesy against Yahoo's
# undocumented rate limiting, since this can touch thousands of tickers in one run.
REQUEST_DELAY_SECONDS = 0.2

STOCKANALYSIS_NOTE = (
    "price(s) sourced from stockanalysis.com (Close, not Open) - yfinance has no data at "
    "all for this ticker, likely delisted/acquired outright"
)


def _target_dates(trade, today):
    """Maps each of a trade's still-unset price columns to the date it needs, skipping any
    horizon that hasn't arrived yet."""
    transaction_date = datetime.date.fromisoformat(trade.transaction_date)
    targets = {}
    if trade.price_at_transaction is None:
        targets["price_at_transaction"] = transaction_date
    if trade.price_at_notification is None:
        targets["price_at_notification"] = datetime.date.fromisoformat(trade.notification_date)
    for column, days in models.HORIZON_COLUMNS:
        if getattr(trade, column) is not None:
            continue
        horizon_date = transaction_date + datetime.timedelta(days=days)
        if horizon_date <= today:
            targets[column] = horizon_date
    return targets


def backfill(conn, ticker_limit=None, use_stockanalysis_fallback=False):
    today = datetime.date.today()
    trades = models.get_trades_needing_prices(conn)
    by_ticker = defaultdict(list)
    for trade in trades:
        by_ticker[trade.ticker].append(trade)
    if ticker_limit is not None:
        by_ticker = dict(list(by_ticker.items())[:ticker_limit])
    total_trades = sum(len(v) for v in by_ticker.values())

    summary = {
        "tickers": len(by_ticker), "tickers_no_data": 0, "tickers_via_fallback": 0,
        "trades_touched": 0, "prices_set": 0,
    }
    print(f"{total_trades} trade(s) across {len(by_ticker)} ticker(s) need at least one price.")

    for ticker, ticker_trades in by_ticker.items():
        earliest = min(datetime.date.fromisoformat(t.transaction_date) for t in ticker_trades)
        history = fetch_ticker_history(ticker, earliest, today)
        time.sleep(REQUEST_DELAY_SECONDS)

        used_fallback = False
        if history is None and use_stockanalysis_fallback:
            history = fetch_ticker_history_stockanalysis(ticker)
            time.sleep(REQUEST_DELAY_SECONDS)
            used_fallback = history is not None

        if history is None:
            print(f"{ticker}: no data from yfinance (delisted/renamed/typo?) - skipping "
                  f"{len(ticker_trades)} trade(s).")
            summary["tickers_no_data"] += 1
            continue
        if used_fallback:
            print(f"{ticker}: recovered via stockanalysis.com fallback (Close, not Open).")
            summary["tickers_via_fallback"] += 1

        any_touched = False
        for trade in ticker_trades:
            prices = {}
            for column, target_date in _target_dates(trade, today).items():
                price = history.price_on_or_after(target_date)
                if price is not None:
                    prices[column] = price
            if prices:
                models.set_trade_prices(conn, trade.id, prices, commit=False)
                if used_fallback:
                    models.set_trade_reconciliation_note(conn, trade.id, STOCKANALYSIS_NOTE, commit=False)
                summary["prices_set"] += len(prices)
                summary["trades_touched"] += 1
                any_touched = True
        if any_touched:
            conn.commit()

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    parser.add_argument("--ticker-limit", type=int, default=None, help="Process at most N distinct tickers (for testing)")
    parser.add_argument(
        "--use-stockanalysis-fallback", action="store_true",
        help="For tickers yfinance has no data for at all, try stockanalysis.com's "
             "undocumented history API (Close price, not Open). One-time backfill use only "
             "- not intended for the recurring daily catch-up job.",
    )
    args = parser.parse_args()

    conn = models.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = backfill(
        conn, ticker_limit=args.ticker_limit,
        use_stockanalysis_fallback=args.use_stockanalysis_fallback,
    )
    print(f"\nDone: {summary}")


if __name__ == "__main__":
    main()
