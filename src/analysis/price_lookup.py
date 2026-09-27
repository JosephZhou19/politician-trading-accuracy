"""Local-analysis price lookup, backed by ticker_daily_prices instead of a live yfinance
fetch - the single place every analysis script/module gets "what was ticker X's price on or
after date Y" from, replacing the old pre-computed point-price columns on trades
(price_at_transaction/notification/30/90/180/365d, removed - see PLAN.md).

Bulk-loads per ticker (one query, not one per trade) into a TickerHistory, reusing its
price_on_or_after/daily_prices exactly as-is - those have zero I/O dependency, they only
operate on the pandas Series handed to them, so this is just a different way of building
that Series (from a local table instead of a live fetch).
"""
from __future__ import annotations

import datetime
import sqlite3
from collections import defaultdict

import pandas as pd

from src.market.prices import TickerHistory

_QUERY_BATCH_SIZE = 500


def load_price_histories(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, TickerHistory]:
    """Bulk-loads every given ticker's full ticker_daily_prices series into a TickerHistory,
    a handful of queries total (chunked to stay under SQLite/Turso's bound-parameter limit)
    rather than one query per ticker. A ticker with zero stored rows is simply absent from
    the result - callers should treat that the same as "can't price this", same as a None
    return from the old live-fetch path (fetch_ticker_history)."""
    if not tickers:
        return {}
    by_ticker: dict[str, list[tuple[datetime.date, float]]] = defaultdict(list)
    unique_tickers = list(dict.fromkeys(tickers))
    for start in range(0, len(unique_tickers), _QUERY_BATCH_SIZE):
        batch = unique_tickers[start:start + _QUERY_BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT ticker, date, price FROM ticker_daily_prices WHERE ticker IN ({placeholders})",
            batch,
        ).fetchall()
        for row in rows:
            by_ticker[row["ticker"]].append((datetime.date.fromisoformat(row["date"]), row["price"]))

    histories: dict[str, TickerHistory] = {}
    for ticker, pairs in by_ticker.items():
        pairs.sort(key=lambda p: p[0])
        dates = [d for d, _ in pairs]
        prices = [p for _, p in pairs]
        histories[ticker] = TickerHistory(ticker, pd.Series(prices, index=pd.Index(dates)))
    return histories
