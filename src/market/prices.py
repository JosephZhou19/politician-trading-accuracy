"""Historical stock prices via yfinance, one API call per ticker covering the whole range
of dates a caller needs - a trade's several price points (transaction, notification,
30/90/180/365-day horizons) share the same ticker's history instead of one call per date.

Uses yfinance's default auto-adjusted Open price - confirmed split/dividend-adjusted
against AAPL's real 2020 4-for-1 split (continuous across the split date, no jump), so a
value here stays comparable across a stock split during the holding period.
"""
from __future__ import annotations

import datetime
import math

import pandas as pd
import requests
import yfinance as yf


def _normalize_ticker(ticker: str) -> str:
    """Yahoo uses a hyphen for share classes (BRK-B); disclosed filings often use a period
    (BRK.B) instead."""
    return ticker.replace(".", "-")


class TickerHistory:
    """One ticker's fetched daily price series, with date-or-next-trading-day lookup.

    price_type is "open" for the normal yfinance path, or "close" for the
    fetch_ticker_history_stockanalysis fallback (that source has no Open column) - callers
    that care about the distinction (e.g. to flag it on a trade) can check this."""

    def __init__(self, ticker: str, opens, price_type: str = "open"):
        self.ticker = ticker
        self.price_type = price_type
        self._opens = opens  # pandas Series: price, indexed by ascending datetime.date

    # A real market gap (weekend, holiday cluster) never exceeds a handful of days. A
    # much larger gap between target_date and the nearest available data means this
    # ticker's history doesn't actually cover that era at all - confirmed this happens for
    # real via a recycled ticker symbol (MON: the real Monsanto delisted in 2018, but a
    # different, unrelated company started trading under the same "MON" symbol in 2021 -
    # querying MON for a 2014 date, with no cap, silently returned that unrelated company's
    # price as if it were Monsanto's). Reject rather than silently return a wrong match.
    MAX_ROLL_FORWARD_DAYS = 10

    def price_on_or_after(self, target_date: datetime.date) -> float | None:
        """Open price on target_date, or the next trading day if it falls on a weekend or
        market holiday. Skips past a trading day with no real Open (a halt or data gap -
        confirmed this happens on real data) rather than returning NaN. None if there's no
        valid price within MAX_ROLL_FORWARD_DAYS of target_date - either the history ends
        before target_date, or (a recycled ticker symbol) it doesn't cover that era at all."""
        idx = self._opens.index.searchsorted(target_date)
        while idx < len(self._opens):
            found_date = self._opens.index[idx]
            if (found_date - target_date).days > self.MAX_ROLL_FORWARD_DAYS:
                return None
            price = self._opens.iloc[idx]
            if not math.isnan(price):
                return float(price)
            idx += 1
        return None


def fetch_ticker_history(
    ticker: str, start_date: datetime.date, end_date: datetime.date
) -> TickerHistory | None:
    """Fetches one ticker's daily Open price for [start_date, end_date]. Returns None if
    the ticker has no data at all in that range (delisted, renamed - e.g. Yahoo serves no
    history at all under the dead "FB" symbol, only "META" - or a disclosure typo) rather
    than raising; a caller should treat that as "can't price this trade", not a crash.

    end_date is padded by a few days past yfinance's exclusive end-of-range so the exact
    end_date requested is actually included, and so a target that lands on end_date itself
    can still roll forward to its next trading day.
    """
    normalized = _normalize_ticker(ticker)
    padded_end = end_date + datetime.timedelta(days=5)
    history = yf.Ticker(normalized).history(start=start_date, end=padded_end)
    if history.empty:
        return None
    opens = history["Open"]
    opens.index = opens.index.date
    return TickerHistory(ticker, opens)


def fetch_ticker_history_stockanalysis(ticker: str) -> TickerHistory | None:
    """Fallback for tickers yfinance has fully purged - confirmed this happens for stocks
    that were acquired/delisted outright (e.g. ATVI after the Microsoft deal closed), not
    just renamed. stockanalysis.com retains the full daily series through the actual
    delisting date via an internal, undocumented chart API (no public API is offered).

    Returns Close price, not Open - that endpoint has no Open column - so a TickerHistory
    from here has price_type="close". One-time use only (the backfill's fallback path for
    the ~1,000 tickers yfinance can't serve at all), not part of the recurring pipeline.
    Returns None on any failure (network, unknown symbol, empty series) rather than raising.
    """
    try:
        resp = requests.get(
            f"https://stockanalysis.com/api/symbol/s/{ticker.lower()}/history",
            params={"type": "chart", "range": "Max"},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return None
    points = payload.get("data")
    if not points:
        return None
    # UTC, not local time - the API's timestamps are UTC-midnight markers, and
    # date.fromtimestamp() (local tz) shifted them back a day on this machine (confirmed:
    # ATVI's real last timestamp landed on 2023-10-12 instead of its actual 2023-10-13).
    dates = [
        datetime.datetime.fromtimestamp(ts / 1000, tz=datetime.timezone.utc).date()
        for ts, _ in points
    ]
    prices = [price for _, price in points]
    closes = pd.Series(prices, index=pd.Index(dates))
    return TickerHistory(ticker, closes, price_type="close")
