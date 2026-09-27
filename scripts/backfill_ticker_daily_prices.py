"""One-time (possibly multi-run) heavy backfill: loads every traded ticker's FULL daily
Open price history into ticker_daily_prices. This is the accepted-as-heavy half of the
two-job split - see update_ticker_daily_prices.py for the cheap recurring catch-up that
keeps it current afterward.

One sequential yfinance call per ticker (same KNOWN_BAD_TICKERS/non-positive-price guards
via fetch_ticker_history as everywhere else in this pipeline) rather than batching - each
ticker needs its OWN start date (its earliest trade date), so there's no shared date range
to batch on the way update_ticker_daily_prices.py's short recurring window has.

Resumable by design, same trickle-cursor reasoning used elsewhere in this pipeline: stops
once TIME_BUDGET_MINUTES is up and picks up right after the last ticker it finished next
run, bookmarked in daily_price_backfill_cursor - because ~3,500 tickers' full history is
genuinely heavy and may take more than one run to finish. This also self-heals across runs
independent of the cursor: a ticker that ends this run with zero stored rows (a fetch
failure, or genuinely no data) is NOT considered "done" (see already_done below), so it's
picked up again on the next invocation regardless of where the cursor points.

Rate-limit posture (yfinance is now the ONLY price data source - there's no Finnhub
fallback anymore for anything price-related): a single fetch failure must not crash the
whole run and lose progress on the remaining tickers (caught and logged, not raised), and a
sustained block (which often shows up as yfinance quietly returning empty results,
indistinguishable from "ticker doesn't exist") must not get silently misread as thousands of
real tickers suddenly having no data - see CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER.

Usage:
    python -m scripts.backfill_ticker_daily_prices --db data/congress_trades.db
"""
import argparse
import datetime
import logging
import sqlite3
import time

from dotenv import load_dotenv

from src.db import models
from src.market.prices import fetch_ticker_history

load_dotenv()

logger = logging.getLogger(__name__)

# A bit more conservative than the old backfill_prices.py's 0.2s - yfinance is now the ONLY
# price data source (no Finnhub fallback for anything price-related anymore), so there's
# more at stake in a rate-limit block than there used to be. Still finishes well inside
# TIME_BUDGET_MINUTES even at ~3,500 tickers.
REQUEST_DELAY_SECONDS = 0.5
DEFAULT_TIME_BUDGET_MINUTES = 75
# If this many tickers in a row come back with nothing, that's not "coincidentally all these
# specific tickers are dead" - every one of them has a real disclosed trade in this DB, so
# they were each tradeable at some point. It's the signature of a sustained yfinance
# rate-limit block instead, which often shows up as quietly-empty responses rather than a
# clean error - not something a fixed per-request delay alone can catch. Stopping here
# avoids grinding through the remaining thousands of tickers making more doomed requests
# (and mislabeling them all as "no data" in the process). Not tuned/backtested itself - a
# documented, revisitable choice, same spirit as SHRINKAGE_K in walkforward_copytrading.py.
CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER = 25


def _rotate_to_resume_point(tickers, cursor):
    """Same rotation used by the daily current-price job's own resume cursor: start right
    after the bookmarked ticker, wrapping to the beginning."""
    if cursor is None:
        return tickers
    start_idx = next((i for i, t in enumerate(tickers) if t > cursor), 0)
    return tickers[start_idx:] + tickers[:start_idx]


def backfill_ticker_daily_prices(conn, ticker_limit=None, time_budget_minutes=DEFAULT_TIME_BUDGET_MINUTES):
    cursor = models.get_daily_price_backfill_cursor(conn)
    tickers = _rotate_to_resume_point(models.get_all_traded_tickers(conn), cursor)
    already_done = models.get_latest_daily_price_dates(conn, tickers)
    tickers = [t for t in tickers if t not in already_done]
    if ticker_limit is not None:
        tickers = tickers[:ticker_limit]

    earliest_by_ticker = models.get_earliest_trade_dates_by_ticker(conn)
    today = datetime.date.today()
    deadline = time.monotonic() + time_budget_minutes * 60 if time_budget_minutes else None

    summary = {"tickers_checked": 0, "tickers_no_data": 0, "days_written": 0, "circuit_breaker_tripped": False}
    print(f"{len(tickers)} ticker(s) still need a full history backfill"
          + (f" (resuming after {cursor!r})." if cursor else "."))

    last_ticker = None
    consecutive_no_data = 0
    for ticker in tickers:
        earliest = earliest_by_ticker.get(ticker, today)
        try:
            history = fetch_ticker_history(ticker, earliest, today)
        except Exception as e:
            # A single ticker's fetch failing (network blip, an HTTP error, a rate-limit
            # response) must not crash the whole run and lose progress on every ticker after
            # it - this one just gets treated as no-data-this-attempt. Since already_done is
            # recomputed fresh from the DB every run (not from the cursor), a ticker that
            # ends up with zero stored rows here gets retried automatically next invocation.
            logger.warning("Skipping %s this run - fetch failed: %r", ticker, e)
            history = None
        time.sleep(REQUEST_DELAY_SECONDS)
        summary["tickers_checked"] += 1

        if history is None:
            summary["tickers_no_data"] += 1
            consecutive_no_data += 1
        else:
            prices = [(date.isoformat(), price) for date, price in history.daily_prices()]
            models.set_ticker_daily_prices(conn, ticker, prices, commit=True)
            summary["days_written"] += len(prices)
            consecutive_no_data = 0

        last_ticker = ticker
        models.set_daily_price_backfill_cursor(conn, ticker)

        if consecutive_no_data >= CONSECUTIVE_NO_DATA_CIRCUIT_BREAKER:
            print(f"{consecutive_no_data} ticker(s) in a row came back with no data - this "
                  f"looks like a yfinance rate-limit block, not {consecutive_no_data} "
                  f"coincidentally-dead tickers. Stopping early rather than mislabeling the "
                  f"rest of the list; will resume after {ticker!r} next run.")
            summary["circuit_breaker_tripped"] = True
            break

        if deadline is not None and time.monotonic() >= deadline:
            print(f"Time budget reached after {summary['tickers_checked']} ticker(s) - "
                  f"stopping early, will resume after {ticker!r} next run.")
            break

    if last_ticker is None:
        print("Nothing left to backfill.")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/congress_trades.db")
    parser.add_argument("--ticker-limit", type=int, default=None, help="Process at most N tickers (for testing)")
    parser.add_argument(
        "--time-budget-minutes", type=float, default=DEFAULT_TIME_BUDGET_MINUTES,
        help="Stop and save the resume cursor after this long (0 to disable and run to completion)",
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Run against the local mirror file at --db, ignoring TURSO_DATABASE_URL/"
             "TURSO_AUTH_TOKEN even if set - avoids per-row Turso round-trip latency during "
             "the heavy one-time load. Push the result to Turso afterward with "
             "scripts/push_ticker_daily_prices_to_turso.py. Needs the local mirror's trades "
             "table already synced (scripts/sync_local_mirror.py) - the ticker universe and "
             "each ticker's earliest trade date come from there.",
    )
    args = parser.parse_args()

    conn = models.connect_local(args.db) if args.local else models.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = backfill_ticker_daily_prices(
        conn, ticker_limit=args.ticker_limit,
        time_budget_minutes=args.time_budget_minutes or None,
    )
    print(f"\nDone: {summary}")


if __name__ == "__main__":
    main()
