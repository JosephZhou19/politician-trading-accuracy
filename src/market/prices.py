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

    # yfinance's own historical Open data can be flat-out garbage for thinly-traded OTC
    # ordinary-share tickers, confirmed for real: DAIUF (Daifuku Co Ltd, currently ~$35)
    # came back as 8e-07 for 2019 dates, AOZOF (Aozora Bank, currently ~$14) as 9.8e-25 -
    # both off by 7-24 orders of magnitude, not just noisy. A legitimate penny stock (GGSM,
    # confirmed real at $0.0024) stays far above this floor, so it's a safe cutoff, not
    # just a round number. Same "reject rather than silently return a wrong match"
    # philosophy as MAX_ROLL_FORWARD_DAYS above.
    MIN_PLAUSIBLE_PRICE = 0.001

    # Same yfinance-garbage-history problem, opposite direction - but NOTE the limits of
    # this check, confirmed by checking real numbers rather than assuming: it only catches
    # astronomical-scale garbage (confirmed: AEXAY at ~10^15, also seen negative - already
    # caught by the floor above). It does NOT catch SUNE ($3.05M-3.4M, a real ~$2-3 stock),
    # NVVE ($1.8M-3.4M), REVB ($1.97M, identical across 36 different calendar dates - real
    # prices don't do that), APVO ($7.7M), OCLCF ($90,684), or PARA (up to $98,001) - all
    # confirmed-bad but too low to threshold on, because BRK.A is a real, actively-traded
    # stock (8 real correctly-priced trades already in this DB, up to $464,947) whose own
    # price keeps climbing (all-time-high $803,783 as of 2025-05, ~13%/year over the last 5
    # years) - any ceiling low enough to catch REVB's $1.97M would leave BRK.A only ~3-4
    # years before a real price got wrongly rejected too. Set high enough (decades of BRK.A
    # headroom even under aggressive growth) that it only ever fires on unambiguous,
    # extreme-magnitude garbage; the $1M-8M-range tickers above need the same manual
    # per-ticker DB cleanup DAIUF/AOZOF got, not a threshold - this is a backstop against a
    # NEW astronomical-scale event on a ticker not yet known to be bad, not a fix for any
    # of the ones already found.
    MAX_PLAUSIBLE_PRICE = 50_000_000

    def price_on_or_after(self, target_date: datetime.date) -> float | None:
        """Open price on target_date, or the next trading day if it falls on a weekend or
        market holiday. Skips past a trading day with no real Open (a halt, data gap, or
        implausible garbage value - all confirmed to happen on real data) rather than
        returning it as-is. None if there's no valid price within MAX_ROLL_FORWARD_DAYS of
        target_date - either the history ends before target_date, or (a recycled ticker
        symbol) it doesn't cover that era at all."""
        idx = self._opens.index.searchsorted(target_date)
        while idx < len(self._opens):
            found_date = self._opens.index[idx]
            if (found_date - target_date).days > self.MAX_ROLL_FORWARD_DAYS:
                return None
            price = self._opens.iloc[idx]
            if not math.isnan(price) and self.MIN_PLAUSIBLE_PRICE <= price <= self.MAX_PLAUSIBLE_PRICE:
                return float(price)
            idx += 1
        return None

    def daily_prices(self) -> list[tuple[datetime.date, float]]:
        """Every (date, price) pair with a real, plausible Open - for bulk-loading a lookup
        table (e.g. benchmark_prices) rather than the point-lookup use price_on_or_after
        serves."""
        return [
            (date, float(price))
            for date, price in self._opens.items()
            if not math.isnan(price) and self.MIN_PLAUSIBLE_PRICE <= price <= self.MAX_PLAUSIBLE_PRICE
        ]


# Tickers where yfinance's own historical data is confirmed garbage (wrong by 100x to
# 10^15x against real-world prices, verified live - not a MIN/MAX_PLAUSIBLE_PRICE gap,
# since several of these sit in a range indistinguishable from a real ultra-high-price
# stock like BRK.A without a threshold that would eventually reject BRK.A itself; see
# MAX_PLAUSIBLE_PRICE's comment). Checked live and confirmed still broken as of 2026-09-20.
# These are real, actively-traded companies - do NOT add them to ticker_prices as
# 'delisted', which would also (wrongly) stop tracking their real current price via
# Finnhub; this list only ever affects the yfinance historical-price path. Skipped before
# the API call entirely (saves the request, not just the bad data). No automatic recheck -
# if yfinance ever fixes its own historical data for one of these, the only cost of not
# noticing is staying unpriced, never a wrong price; revisit manually if that matters.
KNOWN_BAD_TICKERS = frozenset({
    "DAIUF",   # Daifuku Co Ltd - confirmed real ~$35; yfinance returned ~8e-07 to ~4e-05
    "AOZOF",   # Aozora Bank - confirmed real ~$14; yfinance returned ~9e-25 to ~3e-20
    "SUNE",    # SUNation Energy - confirmed real ~$2-3; yfinance returned ~$2.4M-3.4M
    "NVVE",    # Nuvve Holding Corp - confirmed real ~$1-12; yfinance returned ~$1.8M-3.4M
    "APVO",    # Aptevo Therapeutics - confirmed real ~$1-3; yfinance returned ~$7.7M
    "AEXAY",   # Atos Group (ADR) - yfinance returned ~-3e17 to ~3.6e15, both impossible
    "REVB",    # Revelation Biosciences - confirmed real ~$0.9; yfinance returned a flat
               # $1,965,600 across 36 different calendar dates - not real market data
    "OCLCF",   # Oracle Corporation Japan - yfinance returned $90,684 and -$230,171/-$11,468
    "JGCCF",   # JGC Holdings - confirmed real ~$15; yfinance returned -$142.05 (negative)
    "KOSCF",   # KOSE Holdings - confirmed real ~$33; yfinance returned ~-2e-05 (negative)
    "MLPN",    # Credit Suisse X-Links Cushing MLP Infrastructure ETN - yfinance returned
               # an exact 0.0 for an actively-listed security in 2013
    "PARA",    # Paramount Global - confirmed real ~$10-24; yfinance returned $1,479-$98,001
})


def fetch_ticker_history(
    ticker: str, start_date: datetime.date, end_date: datetime.date
) -> TickerHistory | None:
    """Fetches one ticker's daily Open price for [start_date, end_date]. Returns None if
    the ticker has no data at all in that range (delisted, renamed - e.g. Yahoo serves no
    history at all under the dead "FB" symbol, only "META" - or a disclosure typo), it's in
    KNOWN_BAD_TICKERS (yfinance has data, but it's confirmed garbage), or its downloaded
    history contains any day at or below $0 (see the comment below) - either way a caller
    should treat this as "can't price this trade", not a crash.

    end_date is padded by a few days past yfinance's exclusive end-of-range so the exact
    end_date requested is actually included, and so a target that lands on end_date itself
    can still roll forward to its next trading day.
    """
    normalized = _normalize_ticker(ticker)
    if normalized in KNOWN_BAD_TICKERS:
        return None
    padded_end = end_date + datetime.timedelta(days=5)
    history = yf.Ticker(normalized).history(start=start_date, end=padded_end)
    if history.empty:
        return None
    opens = history["Open"]
    opens.index = opens.index.date
    # A single implausible value gets skipped in place by MIN/MAX_PLAUSIBLE_PRICE, but a
    # ticker whose history contains ANY day at or below $0 is a different, worse signal -
    # confirmed live (2026-09) against 3,011 real tickers already in this DB: 6 of the 12
    # known-bad tickers (DAIUF, AOZOF, AEXAY, OCLCF, JGCCF, KOSCF) have hundreds to
    # thousands of zero/negative days apiece, and zero of the 3,011 real tickers have even
    # one - except 2 legitimate money-market funds whose entire "history" is a single $0
    # day, which correctly SHOULD be rejected (they have no real daily price to give). A
    # real stock's Open is never $0 or negative even on its worst day, so this is a safe
    # whole-ticker reject, not a per-day skip - it also catches the next OTC ticker in this
    # class before it needs to be hand-added to KNOWN_BAD_TICKERS.
    if (opens <= 0).any():
        return None
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
