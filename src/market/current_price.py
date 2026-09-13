"""Current stock price via Finnhub's /quote endpoint - the daily trickle job's data source.

Chosen over yfinance for this specific job because Finnhub's free tier has a real published
rate limit (60 calls/min), making a daily full-universe refresh predictable, unlike
yfinance's undocumented and worsening throttling. Unlike yfinance, Finnhub accepts a
ticker's period or hyphen form interchangeably for share classes (confirmed: BRK.B and
BRK-B both return the same quote) - no normalization needed here.
"""
from __future__ import annotations

import os
import time

import requests

BASE_URL = "https://finnhub.io/api/v1/quote"

# Finnhub's free tier hard-caps at 30 calls/sec; staying well under both that and the
# 60/min published limit keeps a multi-thousand-ticker run predictable rather than a gamble.
MIN_SECONDS_BETWEEN_CALLS = 1.1


def fetch_current_price(ticker: str, api_key: str | None = None) -> float | None:
    """Returns the current price, or None if Finnhub has no data for this ticker.

    Finnhub's /quote does not error for a dead symbol - it returns 200 OK with every field
    (including 'c', current price) at 0, a response shape indistinguishable from a
    completely fake ticker (confirmed directly against a known-dead ticker, a live one, and
    a made-up symbol). Callers must never treat a returned 0 as a real price - see
    src.db.models.record_zero_response for how that's handled.
    """
    api_key = api_key or os.environ["FINNHUB_API_KEY"]
    resp = requests.get(BASE_URL, params={"symbol": ticker, "token": api_key}, timeout=10)
    resp.raise_for_status()
    return resp.json().get("c") or None


def fetch_current_prices(tickers, api_key: str | None = None):
    """Yields (ticker, price_or_None) for each ticker, paced to stay under Finnhub's rate
    limit across a long, multi-thousand-ticker run."""
    for ticker in tickers:
        yield ticker, fetch_current_price(ticker, api_key=api_key)
        time.sleep(MIN_SECONDS_BETWEEN_CALLS)
